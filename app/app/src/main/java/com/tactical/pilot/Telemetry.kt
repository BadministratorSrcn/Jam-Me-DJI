package com.tactical.pilot

import dji.sdk.keyvalue.key.DJIKeyInfo
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.key.GimbalKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.key.ProductKey
import dji.v5.manager.KeyManager

/**
 * Registers MSDK telemetry keys and keeps VisualPosClient's DJI fields
 * updated. These values are used for the HUD and the GPS/visual divergence
 * display only — the flight controller never uses DJI GPS.
 */
class TelemetryListener(private val client: VisualPosClient) {

    private val km by lazy { KeyManager.getInstance() }
    private var started = false

    @Volatile var productType: String? = null
    @Volatile var connected: Boolean = false
    @Volatile var flightMode: String? = null
    @Volatile var gpsLevel: String? = null
    @Volatile var satellites: Int? = null
    @Volatile var isFlying: Boolean = false
    @Volatile var virtualStickEnabled: Boolean = false
    @Volatile var gimbalPitch: Double? = null
    @Volatile var lastError: String? = null

    fun start() {
        if (started) return
        started = true
        try {
            productType = km.getValue(KeyTools.createKey(ProductKey.KeyProductType))?.name
        } catch (e: Exception) {
            lastError = "product type: ${e.message}"
        }

        listen(FlightControllerKey.KeyAircraftLocation) { loc ->
            client.djiLat = loc?.latitude
            client.djiLon = loc?.longitude
        }
        listen(FlightControllerKey.KeyAircraftAttitude) { att ->
            client.djiHdg = att?.yaw
        }
        listen(FlightControllerKey.KeyAltitude) { alt ->
            client.djiAlt = alt
        }
        listen(FlightControllerKey.KeyGPSSatelliteCount) { n ->
            satellites = n
            client.djiQuality = n
        }
        listen(FlightControllerKey.KeyGPSSignalLevel) { level ->
            gpsLevel = level?.name
        }
        listen(FlightControllerKey.KeyFlightMode) { mode ->
            flightMode = mode?.name
        }
        listen(FlightControllerKey.KeyVirtualStickControlModeEnabled) { enabled ->
            virtualStickEnabled = enabled == true
        }
        listen(FlightControllerKey.KeyIsFlying) { flying ->
            isFlying = flying == true
        }
        listen(ProductKey.KeyConnection) { conn ->
            connected = conn == true
            if (connected) {
                try {
                    productType = km.getValue(KeyTools.createKey(ProductKey.KeyProductType))?.name
                } catch (_: Exception) {
                }
            }
        }
        listen(GimbalKey.KeyGimbalAttitude) { att ->
            gimbalPitch = att?.pitch
        }
    }

    private fun <T> listen(info: DJIKeyInfo<T>, apply: (T?) -> Unit) {
        try {
            km.listen(KeyTools.createKey(info), this) { _, newValue ->
                try {
                    apply(newValue)
                } catch (e: Exception) {
                    lastError = "telemetry apply: ${e.message}"
                }
            }
        } catch (e: Exception) {
            status.logLine("telemetry listen failed: ${e.message?.take(70)}")
        }
    }

    fun hudLines(): String = buildString {
        appendLine("MSDK   ${if (App.registered) "REGISTERED" else "NOT REGISTERED"}  " +
                "product ${if (App.productConnected) "CONNECTED" else "—"}  ${productType ?: ""}")
        appendLine("FLIGHT ${flightMode ?: "—"}  flying=$isFlying  " +
                "VS=${if (virtualStickEnabled) "ON" else "off"}")
        appendLine("GPS    ${gpsLevel ?: "—"}  sats=${satellites ?: "—"}  " +
                "(reference only — never used for control)")
        appendLine("GIMBAL pitch=${gimbalPitch?.let { "%.1f°".format(it) } ?: "—"}")
        lastError?.let { appendLine("error: $it") }
    }
}
