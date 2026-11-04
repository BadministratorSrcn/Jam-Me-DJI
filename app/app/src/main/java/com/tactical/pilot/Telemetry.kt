package com.tactical.pilot

/**
 * Registers MSDK telemetry keys and keeps VisualPosClient's DJI-GPS fields
 * updated. Skeleton contract:
 *
 *   FlightController keys (KeyManager):
 *     - GPSLocation (lat/lon)         → client.djiLat/lon
 *     - Compass.heading / GpsInfo     → client.djiHdg / djiQuality
 *     - Altitude / VFR data           → client.djiAlt
 *     - Connection state              → status.logLine(...)
 *
 * Reference implementations live in the official MSDK V5 sample under
 * `SampleTreeNode`). Replace this stub's TODOs with real KeyManager listeners
 * when the AAR dependencies are wired in (docs/ARCHITECTURE.md §4).
 */
class TelemetryListener(private val client: VisualPosClient) {

    fun start() {
        status.logLine("Telemetry: attach KeyManager listeners (TODO: wire AAR)")
        // TODO: KeyManager.getInstance().listen(KeyInfo.create(KEY_GPS_LOCATION, ...)) { ... }
    }

    /** CSV line appended continuously; later stages can convert to geotags. */
    fun logCsvRow() {
        // SKELETON: append to app-internal storage:
        // ts, dji_lat, dji_lon, dji_hdg, dji_alt, dji_gps_q,
        //     vis_lat, vis_lon, vis_conf, vis_src, fix_valid
    }
}
