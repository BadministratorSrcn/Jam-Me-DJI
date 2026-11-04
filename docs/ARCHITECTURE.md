# Jam-Me-DJI — Architecture & DJI Constraints

## 1. End-to-end data flow (bridge, phase 1)

```
Mini 4 Pro camera
   │ (camera live stream, set by LiveStreamManager in the Android app)
   ▼ RTMP over WiFi/LAN  (latency ≈ 200-500 ms)
Ground PC  ── ffmpeg ──► BGR frames ──► visual core (submodule)
   │                                        │ TileLocalizer.match_frame()
   │                                        ▼
   │                       Kalman ──► {lat, lon, conf, src, valid}
   │                                        ▼
   │                     HTTP API (ThreadingHTTPServer, stdlib)
   │                        GET /status   GET /snapshot  GET /health
   ▼                                         ▲
Android app (RC phone)                       │ poll 2-5 Hz
   ├ VisualPosClient  ───────────────────────┘
   ├ HUD overlay: DJI-GPS (MSDK telemetry) vs visual fix
   ├ CSV logger: t, dji_lat/lon/hdg/alt/gps_quality, vis_lat/lon/conf/src
   └ (phase 2) mission runner: WaypointMission / virtual-stick gate
```

## 2. /status JSON schema

```json
{
  "ts": 1730750000.123,
  "age_s": 0.21,
  "lat": 37.914, "lon": 40.229,
  "heading_deg": 12.4,
  "src": "visual|optical_flow|none",
  "conf": 0.82,
  "matches": 64,
  "tile": "1_1",
  "fix_valid": true,
  "stale_reason": "",
  "dgi": {}          // optional mirror of android-side DJI telemetry (phase 2)
}
```

`fix_valid` mirrors the Pi repo's stale-fix watchdog: positions older than
`position_timeout` are reported invalid (GGA quality 0 there / `fix_valid:false`
here). Same thresholds live in the core config.

## 3. The four DJI hard constraints (why this architecture)

1. **No position injection.** DJI's flight controller ignores/never accepts
   external GNSS narrative (NMEA/UBX). Our Pi-side trick (`GPS_TYPE=5`,
   NMEA over serial) has no DJI counterpart — firmware is sealed.
2. **Safety gates on control.** `FlyTo`/`WaypointMission`/`VirtualStick`
   executions depend on the aircraft's own positioning state. With GNSS
   denied, DJI products transition to vision-system hold / RTH and refuse
   API-controlled navigation. Expected behavior must be treated as
   *monitoring*, not control, on consumer aircraft.
3. **Mini 4 Pro `VideoFeed` bug.** `onReceiveStreamData` doesn't fire
   (issue #641, MSDK 5.16). The `LiveStreamManager` RTMP/RTSP path is the
   workaround for pixels reaching the ground PC. If DJI later fixes
   VideoFeed, phase 2 (full Kotlin port) can move the core onto the
   RC-phone.
4. **RC constraints.** RC-N2 supported since MSDK v5.14; RC-N3 landed in
   v5.17 (Nov 2025). Target MSDK ≥ 5.17 and match RC models in the app's
   startup checks.

## 4. Build notes (Android)

- Minimum Android 7 (SDK 24), target 34; Kotlin 1.9; minSdk of the MSDK
  sample is the reference.
- MSDK V5 dependency options:
  - AAR files from the official sample `dev-sdk-main` (`api-<version>.aar`,
    `components-*.aar`, UXSdk if desired) → copy to `app/libs/`.
  - or jitpack-style coordinates if you keep them in your own repo.
- Register the app key before any SDK call:
  `MSDKApplication`-style init in `App.kt`, then
  `SDKManager.getInstance().init(...)`.
- App must stay foreground/screen-on while streaming (foreground service
  `AndroidManifest` entry included with `FOREGROUND_SERVICE` +
  `INTERNET` etc.).
- Follow DJI requirements: SDK registration on internet connection
  (first launch), runtime permissions list, and DJI's usage policy.

## 5. Phase 2 (on-phone core) — plan

- Port `common.PositionKalman` + `TileLocalizer` to Kotlin using
  OpenCV for Android (org.opencv).
- Feature cache format reuse: `.npz` → rewrite using plain binary
  (simple header + floats) so both Python and Android can read it
  without numpy.
- Frames: once VideoFeed is fixed, subscribe and feed the matcher.
- Fallback if VideoFeed still broken: decode the RTMP stream **on the
  phone** (libstreaming/ffmpeg-kit) so bridge mode still benefits.

## 6. Safety

- Visual fix is advisory; DJI firmware remains authoritative.
- Never disable obstacle/RTH protections for the experiment.
- Log divergence; if DJI GPS and visual fix disagree persistently in
  GPS-good conditions, stop flying and re-verify the tile georeferencing.
