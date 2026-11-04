package com.tactical.pilot

import android.os.SystemClock
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Polls the ground-PC bridge (/status and /drive) and keeps the HUD data.
 *
 * The bridge is the only source of navigation feedback. DJI's own GPS is
 * read for the divergence display and is never sent back into the loop.
 */
class VisualPosClient(var host: String, var port: Int) {

    // DJI telemetry (filled by TelemetryListener)
    @Volatile var djiLat: Double? = null
    @Volatile var djiLon: Double? = null
    @Volatile var djiHdg: Double? = null
    @Volatile var djiAlt: Double? = null
    @Volatile var djiQuality: Int? = null

    @Volatile var lastStatus: JSONObject? = null
    @Volatile var lastDrive: JSONObject? = null
    @Volatile var lastStatusAt = 0L
    @Volatile var lastDriveAt = 0L
    @Volatile var lastError: String? = null
    @Volatile var lastPollOk = false

    val baseUrl: String get() = "http://$host:$port"

    fun driveAgeMs(): Long =
        if (lastDriveAt == 0L) Long.MAX_VALUE else SystemClock.elapsedRealtime() - lastDriveAt

    fun statusAgeMs(): Long =
        if (lastStatusAt == 0L) Long.MAX_VALUE else SystemClock.elapsedRealtime() - lastStatusAt

    fun poll(): Boolean {
        return try {
            val status = JSONObject(get("/status"))
            lastStatus = status
            lastStatusAt = SystemClock.elapsedRealtime()

            val drive = JSONObject(get("/drive"))
            lastDrive = drive
            lastDriveAt = SystemClock.elapsedRealtime()

            lastError = null
            lastPollOk = true
            true
        } catch (e: Exception) {
            lastError = e.message?.take(100) ?: e.javaClass.simpleName
            lastPollOk = false
            false
        }
    }

    fun refreshOnce() = poll()

    fun hudText(): String {
        val st = lastStatus ?: return "bridge: no data yet (${lastError ?: "connecting"})"
        val est = st.optJSONObject("estimated")
        val fixValid = st.optBoolean("fix_valid", false)
        val d = lastDrive
        val age = driveAgeMs()

        val visual = if (est != null) {
            "VISUAL lat %.7f  lon %.7f  conf %.2f  matches %d  %s  tile %s".format(
                est.optDouble("lat", 0.0),
                est.optDouble("lon", 0.0),
                est.optDouble("conf", 0.0),
                est.optInt("matches", 0),
                est.optString("src", "?"),
                est.optString("tile", "?")
            )
        } else "VISUAL no fix"

        val dji = if (djiLat != null && djiLon != null) {
            "DJI    lat %.7f  lon %.7f  sats %s  hdg %s".format(
                djiLat, djiLon, djiQuality ?: "—",
                djiHdg?.let { "%.0f°".format(it) } ?: "—"
            )
        } else "DJI    telemetry pending"

        val div = divergence(est)
        val cmd = if (d != null) {
            "CMD    %s  dist %s m  vf %+.2f  vr %+.2f  blind %s  age %s".format(
                d.optString("mode", "?"),
                d.optDouble("dist_m", Double.NaN).let { if (it.isNaN()) "—" else "%.2f".format(it) },
                d.optDouble("v_forward", 0.0),
                d.optDouble("v_right", 0.0),
                d.optBoolean("blind", false),
                ageText(age)
            )
        } else "CMD    no drive data"

        return buildString {
            appendLine(visual)
            appendLine("FIX    ${if (fixValid) "VALID" else "INVALID"}  status age ${ageText(statusAgeMs())}")
            appendLine(dji)
            appendLine("DIVERGENCE  ${div?.let { "%.1f m".format(it) } ?: "—"}  (visual vs DJI GPS, reference only)")
            appendLine(cmd)
            if (!lastPollOk) appendLine("bridge err: ${lastError ?: "unreachable"}")
        }
    }

    private fun ageText(ms: Long): String = if (ms == Long.MAX_VALUE) "—" else "$ms ms"

    private fun divergence(est: JSONObject?): Double? {
        val vLat = est?.optDouble("lat", Double.NaN) ?: return null
        val vLon = est.optDouble("lon", Double.NaN)
        if (vLat.isNaN() || vLon.isNaN()) return null
        val a = djiLat ?: return null
        val b = djiLon ?: return null
        val mLat = (vLat - a) * 111320.0
        val mLon = (vLon - b) * 111320.0 * Math.cos(Math.toRadians(vLat))
        return Math.hypot(mLat, mLon)
    }

    private fun get(path: String): String {
        val connection = URL(baseUrl + path).openConnection() as HttpURLConnection
        connection.connectTimeout = 1200
        connection.readTimeout = 1200
        connection.requestMethod = "GET"
        connection.useCaches = false
        return try {
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
    }
}
