# Jam-Me-DJI — Architecture & DJI Constraints

## 1. End-to-end data flow

```
Mini 4 Pro camera (nadir)
   │ (camera live stream, set by LiveStreamManager in the Android app)
   ▼ RTMP over WiFi/LAN  (latency ≈ 200-500 ms)
Ground PC ── ffmpeg ──► BGR frames ──► visual core (submodule)
   │                                        │ TileLocalizer.match_frame()
   │                                        ▼
   │                       Kalman ──► raw visual fix
   │                                        │
   │                       /calibrate offset (operator anchor)
   │                                        ▼
   │                              calibrated fix {lat,lon,conf,src}
   │                                        │
   │                     ┌──────────────────┴──────────────────┐
   │                     │ GPS-free controller                  │
   │                     │ go / hold / mission                  │
   │                     │ stale fix → zero velocities          │
   │                     └──────────────────┬──────────────────┘
   │                                        ▼
   │                     HTTP API (ThreadingHTTPServer, stdlib)
   │        GET /status /drive /mission /snapshot /health
   │        POST /command /calibrate /mission /settings
   ▼                                         ▲
Ground PC GUI (control_gui.py)               │ poll ~4 Hz
   ├ satellite mosaic (true tile corners)     │
   ├ click target / double-click GO           │
   ├ position calibration + mission queue     │
   └ camera HUD + event log                   │
                                             │
Android app (RC phone)                       │ poll ~10 Hz (/drive)
   ├ VisualPosClient ────────────────────────┘
   ├ VirtualStickRunner → DJI virtual stick
   ├ HUD overlay: DJI-GPS vs visual fix
   └ CSV logger
```

The PC can run the whole flight alone (bridge + GUI). The Android app is
needed only as the radio link for the camera stream and the virtual-stick
actuator.

## 2. Control loop (GPS-free)

All decisions use the visual fix; DJI GPS is never an input.

| mode | behavior |
|---|---|
| `idle` | zero velocities, no target |
| `go` | `v = min(kp * dist, vmax)` toward the target, decomposed into body frame (forward = camera heading, right = 90° clockwise) |
| `hold` | target = fix at the moment HOLD is pressed; below `hold_deadband` velocities are zero |
| mission | same as `go`, auto-advances to the next waypoint when within `stop_radius`; completes after the last one |
| blind | if an active `go`/`hold`/mission loses a valid fix, velocities are forced to zero and `blind:true` is published |

`fix_valid` is the stale-fix watchdog: `now - last_fix_ts <=
position_timeout` (core config). The Android runner obeys `/drive`
verbatim, so a stale fix can never produce motion.

Optical flow is only a short bridge across blur/dim dropouts
(`of_max_age`, default 3 s). After a longer blind gap, the first full
tile match must pass a stronger gate (`reacq_min_matches`,
`reacq_min_confidence`) before the fix becomes valid again; weak or
jumpy matches are rejected instead of waking the controller.

Settings (`vmax`, `kp`, `stop_radius`, `hold_deadband`) can be changed
live with `POST /settings`; the GUI exposes the speed limit slider.

## 3. Calibration semantics

`POST /calibrate {lat, lon}` is a **manual anchor**, not GPS injection:

1. Operator reads the drone's actual current coordinate.
2. Bridge computes `offset = entered - raw_visual_fix`.
3. Every future visual fix is shifted by that offset.

The closed loop continues on tile matching only. This corrects a
systematic bias between the tile mosaic and the real world (e.g. imagery
offset), and it is what makes the displayed distance/bearing match the
operator's coordinate reference. `{"action":"clear"}` removes the offset.

## 4. HTTP API schema

### `GET /status`

```json
{
  "ts": 1762290000.12,
  "age_s": 0.18,
  "estimated": {
    "lat": 37.9072380, "lon": 40.2349231,
    "conf": 0.91, "matches": 184,
    "heading": 54.2, "tile": "1_1", "src": "visual"
  },
  "fix_valid": true,
  "tiles_loaded": 16,
  "mode": "go",
  "ctrl": {"vmax": 2.5, "kp": 0.6, "stop_radius": 1.5,
           "hold_deadband": 0.35},
  "cal_offset": {"dlat": 0.0001, "dlon": -0.00008, "enabled": true},
  "mission": {"points": [[37.9071, 40.2349]], "idx": 0,
              "active": true, "current": [37.9071, 40.2349],
              "count": 1},
  "area": {"lat": [37.9007, 37.9138], "lon": [40.2306, 40.2471]}
}
```

`src` is `visual`, `optical_flow`, or absent when no fix exists.
Positions older than `position_timeout` set `fix_valid:false`.

### `GET /drive`

```json
{
  "id": 42,
  "mode": "go",
  "target": [37.9071, 40.2349],
  "v_forward": 1.82,
  "v_right": -0.34,
  "dist_m": 6.1,
  "bearing_deg": 54.2,
  "blind": false,
  "note": "velocities are body-frame (forward=along heading); ..."
}
```

`v_forward`/`v_right` are m/s in body frame. The Android app polls this
at ~10 Hz and converts to virtual-stick pitch/roll.

### POST endpoints

| endpoint | body | effect |
|---|---|---|
| `/command` | `{"action":"go","lat":..,"lon":..}` | fly to target (must be inside the loaded area) |
| `/command` | `{"action":"hold"}` | hold the current fix |
| `/command` | `{"action":"stop"}` | zero velocities, cancel mission |
| `/calibrate` | `{"lat":..,"lon":..}` | anchor visual fix to a known coordinate |
| `/calibrate` | `{"action":"clear"}` | remove the offset |
| `/mission` | `{"action":"set","points":[[lat,lon],...]}` | replace the queue (does not start; empty list clears) |
| `/mission` | `{"action":"start"}` / `"skip"` / `"clear"` | run / advance / empty the queue |
| `/settings` | any of `vmax`,`kp`,`stop_radius`,`hold_deadband` | live controller tuning |

Targets and waypoints outside the tile area are rejected with `400` and
the valid `area` bounds.

### GET helpers

| endpoint | description |
|---|---|
| `/mission` | mission queue JSON only |
| `/snapshot` | latest camera frame (JPEG; `204` until the first frame) |
| `/health` | `200` when a fix exists, `503` otherwise |

## 5. The four DJI hard constraints (why this architecture)

1. **No position injection.** DJI's flight controller ignores/never accepts
   external GNSS narrative (NMEA/UBX). Our Pi-side trick (`GPS_TYPE=5`,
   NMEA over serial) has no DJI counterpart — firmware is sealed.
2. **Safety gates on control.** `FlyTo`/`WaypointMission`/`VirtualStick`
   executions depend on the aircraft's own positioning state. With GNSS
   denied, DJI products transition to vision-system hold / RTH and refuse
   API-controlled navigation. The bridge therefore treats every command
   as best-effort: if the aircraft refuses, it holds; our stack never
   assumes the velocity was applied.
3. **Mini 4 Pro `VideoFeed` bug.** `onReceiveStreamData` doesn't fire
   (issue #641, MSDK 5.16). The `LiveStreamManager` RTMP/RTSP path is the
   workaround for pixels reaching the ground PC. If DJI later fixes
   VideoFeed, phase 2 (full Kotlin port) can move the core onto the
   RC-phone.
4. **RC constraints.** RC-N2 supported since MSDK v5.14; RC-N3 landed in
   v5.17 (Nov 2025). Target MSDK ≥ 5.17 and match RC models in the app's
   startup checks.

## 6. Build notes (Android)

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
- `VirtualStickRunner` is a stub: wire the official virtual-stick keys,
  then verify pitch/roll signs on the bench before any flight. The sign
  constants are marked `VERIFY ON HARDWARE`.

## 7. Phase 2 (on-phone core) — plan

- Port `common.PositionKalman` + `TileLocalizer` to Kotlin using
  OpenCV for Android (org.opencv).
- Feature cache format reuse: `.npz` → rewrite using plain binary
  (simple header + floats) so both Python and Android can read it
  without numpy.
- Frames: once VideoFeed is fixed, subscribe and feed the matcher.
- Fallback if VideoFeed still broken: decode the RTMP stream **on the
  phone** (libstreaming/ffmpeg-kit) so bridge mode still benefits.

## 8. Safety

- Visual fix is advisory to DJI's own flight controller; DJI firmware
  remains authoritative.
- Never disable obstacle/RTH protections for the experiment.
- A stale fix must always produce zero velocity — never fly blind.
- Optical flow must not dead-reckon indefinitely (`of_max_age`), and a
  fix lost for longer than `position_timeout` must pass the stronger
  reacquisition gate before commands resume.
- Log divergence; if DJI GPS and visual fix disagree persistently in
  GPS-good conditions, stop flying and re-verify the tile georeferencing.
- Verify all command signs (virtual-stick pitch/roll) on the bench with
  propellers removed before flight.
