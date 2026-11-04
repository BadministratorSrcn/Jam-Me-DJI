# Jam-Me-DJI — Visual GPS-Navigation for DJI Mini 4 Pro (MSDK V5)

Sibling project of [`Jam-Me`](https://github.com/BadministratorSrcn/Jam-Me)
(Raspberry Pi + ArduPilot). Same visual localization core, different
platform.

> ⚠️ Educational/research only. GPS simulation & spoofing is illegal in
> many jurisdictions. GPS-denied flight ops: you are solely responsible
> for legality/safety. See the main repo's disclaimer.

---

## Why a "bridge" architecture — the DJI reality (researched Nov 2025)

Unlike ArduPilot, **you cannot feed synthetic NMEA position into a DJI
flight controller**. DJI firmware owns its EKF:

| Capability | ArduPilot (Pi repo) | DJI Mini 4 Pro (this repo) |
|---|---|---|
| Inject GPS to FC (`GPS_TYPE=5`) | ✅ NMEA over serial | ❌ closed firmware |
| GPS-less virtual stick commands | ✅ (own EKF accepts our fix) | ⚠️ DJI safety gates virtual stick/`FlyTo` on positioning state — expect refusal when GPS is denied |
| Real-time camera frames via MSDK `VideoFeed` | ✅ own camera | ⚠️ bug: `onReceiveStreamData` never fires on Mini 4 Pro (issue dji-sdk/Mobile-SDK-Android-V5#641, MSDK 5.16) |
| Camera **RTMP live stream** via MSDK `LiveStreamManager` | ✅ | ✅ available — RTMP/RTSP/GB28181 modes |
| MSDK V5 aircraft support | ✅ | ✅ Mini 4 Pro (v5.14+, Mar 2025); RC-N3 support landed v5.17 |

**Therefore:** this project keeps the full visual core on the ground PC
(reused as a git submodule — shared literally with the Pi repo), while an
Android MSDK app on the DJI remote streams camera video (RTMP) and
telemetry to the PC, and consumes the visual fix back over HTTP:

```
┌─────────────────────────┐         ┌────────────────────────────────┐
│ Android (RC-N2/N3 phone)│   WiFi  │ Ground PC                      │
│  Jam-Me-DJI app         │         │  bridge_server.py              │
│  ├ MSDK V5 (Mini 4 Pro) │◄────────┤  ├ RTMP ingest (ffmpeg)        │
│  ├ LiveStreamManager ───┼─RTMP───►│  ├ visual core (submodule):    │
│  │    RTMP out          │         │  │  TileLocalizer+Kalman        │
│  ├ telemetry recorder   │         │  ├ GPS-free controller         │
│  ├ VisualPosClient ─────┼──HTTP──►│  │  go / hold / mission        │
│  └ VirtualStickRunner   │         │  └ control_gui.py (operator)   │
│     (10 Hz /drive)      │         │     map · camera · calibration │
└─────────────────────────┘         └────────────────────────────────┘
```

Ground PC runs against pre-loaded satellite tiles (`sim_tiles_*` /
`tiles/` from the Pi repo flow — same format). The operator can fly the
whole mission from the PC: click a destination on the satellite map,
press GO, or queue waypoints.

**Control loop:** the bridge computes body-frame velocities from the
visual fix only (`/drive`), the phone sends them through DJI virtual
stick. If the visual fix goes stale, the bridge publishes **zero
velocities** and flags `blind` — there is no blind flight mode.

**Honest mission profile on DJI:** the GPS-free servo is implemented and
tested against the simulator, but DJI firmware still gates virtual-stick
control on its own positioning state. With GPS present → normal
operation; when GPS degrades → DJI may hold/RTH and refuse API
navigation. Our stack then reports the true visual position, logs the
divergence, and keeps the last safe command at zero velocity. Direct
visual drive without this gate would be the Mavic-3E/Matrice enterprise
+ PSDK/MAVSDK story, not Mini 4 Pro.

---

## Components

| Path | What it is |
|---|---|
| `pc_bridge/` | Ground PC: RTMP ingest → visual core → GPS-free controller → HTTP API (Python; reuses `common.py`/`main_tile.py` via git submodule) |
| `pc_bridge/control_gui.py` | Tactical operator console: live camera, satellite map, click-to-go, position calibration, waypoint missions |
| `app/` | Android Kotlin MSDK V5 app skeleton (registration, RTMP stream start, telemetry recorder, visual-position client, virtual-stick runner) |
| `docs/ARCHITECTURE.md` | Data flows, message schema, control loop, DJI constraints detail, phase-2 (Kotlin port) plan |

## Quick start (PC part — works today, no drone needed)

```bash
git clone --recurse-submodules https://github.com/BadministratorSrcn/Jam-Me-DJI.git
python -m pip install -r pc_bridge/requirements.txt

# offline demo — bridge consumes the SIMULATED flight (same pipeline!)
python pc_bridge/bridge_server.py --source sim \
    --tiles-dir C:\path\to\Jam-Me\sim_tiles_diyarbakir --port 8080

# operator console (second terminal)
python pc_bridge/control_gui.py \
    --tiles-dir C:\path\to\Jam-Me\sim_tiles_diyarbakir --port 8080
```

Click anywhere on the map to pick a target, press **GO**, and watch the
virtual drone fly there using tile matching alone. `HOLD` station-keeps
on the current fix, `STOP` zeroes velocities. Mission queue, speed limit
and manual position calibration are on the right panel.

Need tiles first? In the Jam-Me repo:

```bash
python simulate.py --mode real --tiles-dir sim_tiles_myarea --no-wait
```

Real drone: the current field path uses **RTSP from the phone**, a local
MJPEG console feed, and an optional **Google Maps JavaScript** window
(WebView2). Follow [`docs/REAL_FLIGHT.md`](docs/REAL_FLIGHT.md):

```powershell
$env:GOOGLE_MAPS_API_KEY="<Maps JavaScript API key>"
.\pc_bridge\run_real.ps1 -TilesDir C:\path\to\Jam-Me\sim_tiles_diyarbakir `
  -Source "rtsp://jamme:jamme@<phone-ip>:8554/streaming/live/1" `
  -MapProvider google
```

The phone shows the exact RTSP URL in its log (`Live555 gen url: ...`).
RTMP + mediamtx remains a fallback (use mediamtx **v1.9.3**; newer builds
fail DJI's librtmp handshake).

## Quick start (Android part)

The MSDK V5.18.0 dependencies are pulled from Maven Central — no manual AAR
download is needed:

```kotlin
implementation("com.dji:dji-sdk-v5-aircraft:5.18.0")
compileOnly("com.dji:dji-sdk-v5-aircraft-provided:5.18.0")
runtimeOnly("com.dji:dji-sdk-v5-networkImp:5.18.0")
```

The App Key `dff381b608c20a4e62589660` and package `com.tactical.pilot` are
already in the manifest. Build:

```powershell
cd app
.\gradlew.bat assembleDebug   # or use the local Gradle 8.9 install
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Connect RC-N2 + Mini 4 Pro, start the app, set the PC IP, press CONNECT,
start RTMP, point the gimbal down, then ARM. Full checklist:
[`docs/REAL_FLIGHT.md`](docs/REAL_FLIGHT.md).

## Status / roadmap

- [x] PC bridge (RTMP ingest + visual core + HTTP API) — works offline & online
- [x] Tactical PC console (map click-to-go, camera HUD, calibration, mission queue)
- [x] GPS-free position servo (GO / HOLD, stale-fix zero-velocity watchdog)
- [x] Manual current-position anchoring (`/calibrate`) and two-point+ missions
- [x] Android app: MSDK 5.18 registration, telemetry, RTMP live stream, gimbal down, real virtual-stick runner
- [ ] First real Mini 4 Pro flight (RC-N2) — bench arm + low hover validation
- [ ] GPS-denied virtual-stick validation (DJI firmware permitting)
- [ ] GPS-mode MSDK WaypointMission runner
- [ ] EXIF geotagging of recorded media with visual fix
- [ ] Phase 2: Kotlin port of visual core (when DJI fixes Mini 4 Pro `VideoFeed`)
- [ ] Optional: enterprise aircraft (Mavic 3E etc.) branch with PSDK position feed

## License

MIT — see LICENSE (same as Jam-Me).
