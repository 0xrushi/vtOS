package dev.vtos.clipshare

import android.content.Context
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Where the T-Deck lives and how to authenticate; stored in SharedPreferences. */
data class Config(val host: String, val port: Int, val token: String) {
    val isComplete get() = host.isNotBlank() && token.isNotBlank()

    fun save(ctx: Context) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("host", host).putInt("port", port).putString("token", token).apply()
    }

    companion object {
        private const val PREFS = "clipshare"
        const val DEFAULT_PORT = 8787

        fun load(ctx: Context): Config {
            val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            return Config(
                p.getString("host", "") ?: "",
                p.getInt("port", DEFAULT_PORT),
                p.getString("token", "") ?: "",
            )
        }
    }
}

/** Talks to clipd on the T-Deck (see docs/CLIPBOARD.md). Blocking: call off the main thread. */
object Sender {
    const val MAX_BYTES = 4096
    private const val TIMEOUT_MS = 5000

    /** Returns null on success, otherwise a short message to show the user. */
    fun send(cfg: Config, text: String): String? {
        val body = text.toByteArray(Charsets.UTF_8)
        if (body.size > MAX_BYTES) return "Too long (${body.size} bytes, max $MAX_BYTES)"
        return request(cfg, "POST", "/clip", body)
    }

    fun ping(cfg: Config): String? = request(cfg, "GET", "/ping", null)

    /** Broadcasts "VTOS?" on the LAN; returns (ip, port) of each T-Deck that answers. */
    fun discover(): List<Pair<String, Int>> {
        val found = LinkedHashMap<String, Int>()
        try {
            DatagramSocket().use { sock ->
                sock.broadcast = true
                sock.soTimeout = 400
                val msg = "VTOS?".toByteArray()
                sock.send(DatagramPacket(msg, msg.size, InetAddress.getByName("255.255.255.255"), Config.DEFAULT_PORT))
                val buf = ByteArray(256)
                val end = System.currentTimeMillis() + 2000
                while (System.currentTimeMillis() < end) {
                    val pkt = DatagramPacket(buf, buf.size)
                    try { sock.receive(pkt) } catch (e: SocketTimeoutException) { continue }
                    val reply = JSONObject(String(pkt.data, 0, pkt.length))
                    if (reply.optString("name") == "vtos") {
                        found[pkt.address.hostAddress ?: continue] = reply.optInt("port", Config.DEFAULT_PORT)
                    }
                }
            }
        } catch (e: Exception) {
            // no Wi-Fi / broadcast blocked: caller shows "none found"
        }
        return found.map { it.key to it.value }
    }

    /** Trades the 6-digit code from `clip pair` for the token. Returns (token, null) or (null, error). */
    fun pair(host: String, port: Int, code: String): Pair<String?, String?> {
        var conn: HttpURLConnection? = null
        return try {
            val body = code.trim().toByteArray()
            conn = (URL("http://${host.trim()}:$port/pair").openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                doOutput = true
                setFixedLengthStreamingMode(body.size)
                outputStream.use { it.write(body) }
            }
            when (val c = conn.responseCode) {
                200 -> JSONObject(conn.inputStream.bufferedReader().readText()).getString("token") to null
                401 -> null to "Wrong code"
                403 -> null to "Pairing not active: run `clip pair` on the T-Deck"
                else -> null to "T-Deck replied HTTP $c"
            }
        } catch (e: IOException) {
            null to "Can't reach T-Deck (${e.javaClass.simpleName})"
        } finally {
            conn?.disconnect()
        }
    }

    private fun request(cfg: Config, method: String, path: String, body: ByteArray?): String? {
        if (!cfg.isComplete) return "Set the address and token first"
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL("http://${cfg.host.trim()}:${cfg.port}$path").openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                setRequestProperty("Authorization", "Bearer ${cfg.token.trim()}")
                if (body != null) {
                    doOutput = true
                    setFixedLengthStreamingMode(body.size)
                    setRequestProperty("Content-Type", "text/plain; charset=utf-8")
                    outputStream.use { it.write(body) }
                }
            }
            when (val code = conn.responseCode) {
                200 -> null
                401 -> "Wrong token"
                413 -> "T-Deck says: too large"
                else -> "T-Deck replied HTTP $code"
            }
        } catch (e: IOException) {
            "Can't reach T-Deck (${e.javaClass.simpleName})"
        } finally {
            conn?.disconnect()
        }
    }
}
