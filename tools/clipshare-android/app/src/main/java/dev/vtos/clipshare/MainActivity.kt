package dev.vtos.clipshare

import android.app.Activity
import android.content.ClipboardManager
import android.content.Context
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import kotlin.concurrent.thread

/** Settings (address, port, token) plus Test and "Send clipboard" buttons. */
class MainActivity : Activity() {
    private lateinit var host: EditText
    private lateinit var port: EditText
    private lateinit var token: EditText
    private lateinit var status: TextView
    private lateinit var code: EditText

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val cfg = Config.load(this)

        host = field("T-Deck address (IP or Tailscale IP)", cfg.host, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        port = field("Port", cfg.port.toString(), InputType.TYPE_CLASS_NUMBER)
        token = field("Token (run `clip token` on the T-Deck)", cfg.token, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS)
        status = TextView(this).apply { setPadding(0, pad, 0, 0) }

        val test = Button(this).apply {
            text = "Test connection"
            setOnClickListener { run("Testing...") { Sender.ping(it) } }
        }
        val sendClip = Button(this).apply {
            text = "Send phone clipboard"
            setOnClickListener {
                val clip = (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                    .primaryClip?.getItemAt(0)?.coerceToText(this@MainActivity)?.toString()
                if (clip.isNullOrEmpty()) status.text = "Clipboard is empty"
                else run("Sending...") { Sender.send(it, clip) }
            }
        }
        code = field("Pairing code (from `clip pair`)", "", InputType.TYPE_CLASS_NUMBER)
        val find = Button(this).apply {
            text = "Find T-Deck & pair"
            setOnClickListener { findAndPair() }
        }
        val hint = TextView(this).apply {
            text = "Then share text from any app and pick \"Send to T-Deck\"."
            setPadding(0, pad, 0, 0)
        }

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            listOf<View>(code, find, host, port, token, test, sendClip, status, hint).forEach { addView(it) }
        })
    }

    private fun field(hint: String, value: String, type: Int) = EditText(this).apply {
        this.hint = hint
        setText(value)
        inputType = type
        setSingleLine()
    }

    /** Broadcast for a T-Deck, then trade the pairing code for the token. */
    private fun findAndPair() {
        val digits = code.text.toString().trim()
        if (digits.length != 6) {
            status.text = "Run `clip pair` on the T-Deck and enter the 6-digit code"
            return
        }
        status.text = "Looking for T-Deck..."
        val manual = current()
        thread {
            // Broadcast finds it on the LAN; otherwise fall back to the typed address (e.g. Tailscale).
            val found = Sender.discover().ifEmpty {
                if (manual.host.isNotBlank()) listOf(manual.host to manual.port) else emptyList()
            }
            if (found.isEmpty()) {
                runOnUiThread { status.text = "No T-Deck found on this Wi-Fi. Enter its address below (e.g. Tailscale IP) and try again." }
                return@thread
            }
            val (ip, p) = found.first()
            val (tok, err) = Sender.pair(ip, p, digits)
            runOnUiThread {
                if (tok == null) {
                    status.text = err
                } else {
                    host.setText(ip); port.setText(p.toString()); token.setText(tok); code.setText("")
                    current().save(this)
                    status.text = "Paired with T-Deck at $ip"
                }
            }
        }
    }

    private fun current() = Config(
        host.text.toString().trim(),
        port.text.toString().toIntOrNull() ?: Config.DEFAULT_PORT,
        token.text.toString().trim(),
    )

    private fun run(label: String, action: (Config) -> String?) {
        val cfg = current()
        cfg.save(this)
        status.text = label
        thread {
            val error = action(cfg)
            runOnUiThread { status.text = error ?: "OK" }
        }
    }
}
