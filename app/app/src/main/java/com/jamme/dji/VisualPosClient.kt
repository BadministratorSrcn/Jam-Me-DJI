package com.jamme.dji

import java.net.HttpURLConnection
import java.net.URL

/**
 * Polls the ground-PC bridge (/status) and renders the HUD text that
 * merges DJI's own GPS reading with our visual fix, computing
 * divergence in meters.
 */
class VisualPosClient(private val host: String, private val port: Int) {

    // >>> DJI side telemetry (filled by TelemetryListener; see Telemetry.kt)
    @Volatile var djiLat: Double? = null
    @Volatile var djiLon: Double? = null
    @Volatile var djiHdg: Double? = null
    @Volatile var djiAlt: Double? = null
    @Volatile var djiQuality: Int? = null

    @Volatile var lastStatus: Map<String, Any?> = emptyMap()
    @Volatile var lastError: String? = null

    fun hudText(): String {
        val st = lastStatus
        val est = st["estimated"] as? Map<String, Any?> ?: return "bridge: no visual fix"
        val vLat = est["lat"] as? Double ?: return "bridge: no fix"
        val vLon = est["lon"] as? Double ?: return "no fix"
        val fmt = { d: Double? -> "%.7f".format(d ?: 0.0) }
        val div = divergence(vLat, vLon)
        val quality = djiQuality?.takeIf { it > 0 } ?: 0
        val valid = st["fix_valid"] as? Boolean == true
        return buildString {
            appendLine("VISUAL  lat ${fmt(vLat)}  lon ${fmt(vLon)}")
            appendLine("        conf ${est["conf"]}  matches ${est["matches"]}  src ${est["src"]}  tile ${est["tile"]}")
            appendLine("DJI     lat ${fmt(djiLat)}  lon ${fmt(djiLon)}  GPS q=$quality")
            appendLine("DIVERGENCE  ${if (div != null) "%.1f m".format(div) else "—"}  [${if (valid) "FIX VALID" else "FIX INVALID"}]")
            lastError?.let { appendLine("bridge err: $it") }
        }
    }

    private fun divergence(vLat: Double, vLon: Double): Double? {
        val a = djiLat ?: return null
        val b = djiLon ?: return null
        val mLat = (vLat - a) * 111320.0
        val mLon = (vLon - b) * 111320.0 * Math.cos(Math.toRadians(vLat))
        return Math.hypot(mLat, mLon)
    }

    fun refreshOnce() = poll()

    fun poll(): Map<String, Any?> {
        try {
            val url = URL("http://$host:$port/status")
            with(url.openConnection() as HttpURLConnection) {
                connectTimeout = 1500
                readTimeout = 1500
                inputStream.bufferedReader().use { r ->
                    lastError = null
                    lastStatus = parse(r.readText())
                }
            }
        } catch (e: Exception) {
            lastError = e.message?.take(80) ?: e.javaClass.simpleName
        }
        return lastStatus
    }

    private fun parse(json: String): Map<String, Any?> {
        // minimal parser (skeleton) — replace with org.json / kotlinx.serialization
        val map = mutableMapOf<String, Any?>()
        Regex("\"est_\\w+\"").find(json) // placeholder: real code uses org.json
        map["fix_valid_typo_true"] = (json.contains("\"fix_valid\":true"))
        val lat = Regex("\"lat\"\\s*:\\s*(-?[0-9.]+)").find(json)?.groupValues?.get(1)
        val lon = Regex("\"lon\"\\s*:\\s*(-?[0-9.]+)").find(json)?.groupValues?.get(1)
        val conf = Regex("\"conf\"\\s*:\\s*([0-9.]+)").find(json)?.groupValues?.get(1)
        val matches = Regex("\"matches\"\\s*:\\s*([0-9]+)").find(json)?.groupValues?.get(1)
        val src = Regex("\"src\"\\s*:\\s*\"([^\"]+)\"").find(json)?.groupValues?.get(1)
        val tile = Regex("\"tile\"\\s*:\\s*\"([^\"]+)\"").find(json)?.groupValues?.get(1)
        map["fix_valid"] = json.contains("\"fix_valid\":true")
        map["estimated"] = mapOf(
            "lat" to lat?.toDouble(), "lon" to lon?.toDouble(),
            "conf" to conf?.toDouble(), "matches" to matches?.toInt(),
            "src" to (src ?: "?"), "tile" to (tile ?: "?"))
        return map
    }
}

private operator fun <K, V> Map<K, V>.get(key: String, default: V): V =
    (this as? Map<String, Any?>)?.let { (it[key] as? V) ?: default } ?: default
