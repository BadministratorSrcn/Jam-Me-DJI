> Shared visual core lives in `core/` — **git submodule** of
> [`BadministratorSrcn/Jam-Me`](https://github.com/BadministratorSrcn/Jam-Me):
>
> ```bash
> git clone --recurse-submodules https://github.com/BadministratorSrcn/Jam-Me-DJI.git
> # already cloned?  git submodule update --init
> # developer override without the submodule:  set JAMME_CORE=...Jam-Me
> ```

# Ground Bridge + Tactical PC Console

The PC is the brain of the DJI build: it ingests the downward camera
stream, localizes against satellite tiles, closes a GPS-free velocity
loop, and publishes body-frame velocities for the phone. It also hosts
the operator console (map, camera, calibration, missions).

## Current real-aircraft path (Nov 2026)

The field-proven path uses **RTSP from the phone**, a local **MJPEG** feed
for the console, and a **Google Maps JavaScript** window (WebView2). RTMP
+ mediamtx remains a fallback only.

```powershell
$env:GOOGLE_MAPS_API_KEY="<key>"     # Maps JavaScript API; never commit it
.\run_real.ps1 `
  -TilesDir C:\...\sim_tiles_diyarbakir `
  -Source "rtsp://jamme:jamme@<phone-ip>:8554/streaming/live/1" `
  -MapProvider google
```

- The phone shows the real URL in its log: `Live555 gen url: ...`
- The bridge reads RTSP directly; `mediamtx` is not needed.
- The console camera uses `--rtsp-url http://127.0.0.1:8080/stream.mjpg`.
- `google_map.py` opens the Google Maps window; it loads `/map` from the
  bridge, which injects the API key server-side.
- The Google window has a camera PiP; the Tkinter console still has the
  LIVE DOWNLINK and all mission/speed controls.
- Google **Map Tiles API is not required** for this window; the existing
  Maps JavaScript API key works. Server-side dynamic tiles via
  `--map-provider google` would require Map Tiles API.
- RTMP fallback: use **mediamtx v1.9.3**. v1.21 fails DJI librtmp with
  `HandShake: client signature does not match`.


```
RTMP / file / sim ──► TileLocalizer ──► Kalman ──► calibrated fix
                                                        │
                                     ┌──────────────────┴──────────────┐
                                     │ GPS-free position servo         │
                                     │ go / hold / mission             │
                                     └──────────────────┬──────────────┘
                                                        ▼
                                            /drive  (body velocities)
                                                        │
                                          Android VirtualStickRunner
```

If the visual fix goes stale for longer than `position_timeout`, the
controller forces **zero velocities** and reports `blind: true`. There
is no blind flight mode.

## Files

| file | purpose |
|---|---|
| `bridge_server.py` | frame ingest → visual core → fix + control API + sim source |
| `control_gui.py` | Tkinter tactical console: live camera, pan/zoom map, target, mission queue, calibration |
| `core/` | git submodule of the Jam-Me repo (TileLocalizer, Kalman, tiles) |

## Install

```bash
python -m pip install -r requirements.txt      # numpy, opencv-python
```

`control_gui.py` uses only Tkinter from the standard library — no extra
GUI dependency.

## Run the offline sandbox (no drone)

Terminal 1 — bridge flying the simulated camera over real ESRI tiles:

```bash
python bridge_server.py --source sim ^
    --tiles-dir C:\path\to\Jam-Me\sim_tiles_diyarbakir --port 8080
```

Terminal 2 — operator console:

```bash
python control_gui.py ^
    --tiles-dir C:\path\to\Jam-Me\sim_tiles_diyarbakir --port 8080
```

The sim starts as a pure control sandbox (`--sim-speed 0`): the virtual
drone only moves when the controller commands velocities. Add
`--sim-speed 10` to make it fly its own route (wind / patrol) while you
still control it.

You can also open `http://127.0.0.1:8080/status` and `/snapshot`
directly in a browser.

## Run with the Mini 4 Pro

1. Start an RTMP server on the PC, e.g. `mediamtx` (port 1935).
2. In the Android app, set the RTMP URL to
   `rtmp://<PC-hotspot-IP>:1935/live` and start streaming.
3. Ingest it:

   ```bash
   python bridge_server.py --source rtmp://127.0.0.1:1935/live ^
       --tiles-dir C:\path\to\tiles --port 8080
   ```

   `ffmpeg` must be on `PATH` (or pass `--ff-path`).
4. Start `control_gui.py` with the same `--tiles-dir` and port.

Wireless tip: PC hosts the hotspot, RC phone joins it → lowest latency.
Mount the camera facing straight down; the visual matcher expects a
nadir view with north-up imagery.

## Operator console

| area | what it does |
|---|---|
| top bar | bridge link, fix validity/confidence, flight mode, clock |
| left | live downward camera with HUD (fix %, lat/lon, mode, target arrow, blind warning) and telemetry tiles |
| center | satellite mosaic, click = target, double-click = GO, drag = pan, wheel = zoom, FIT / DRONE / FOLLOW |
| right | current-position calibration, target lat/lon entry with live distance/bearing, GO / POSITION HOLD / STOP, mission queue, speed limit, event log |

Keyboard: `F5` GO · `F6` HOLD · `F7`/`Esc` STOP · `F` fit map ·
`C` center on drone · `Ctrl+Q` quit.

### Current-position calibration

The visual fix is relative to the loaded tile mosaic. To anchor it to a
known real coordinate:

1. Read the drone's actual current position (map, hand-held GPS, or the
   DJI app when it has a fix).
2. Type it into **CURRENT POSITION → LAT / LON**.
3. Press **SET POSITION**.

The bridge computes the difference against the raw visual fix and adds
that offset to every future fix. The closed loop keeps running on tile
matching only — this is a one-time anchor correction, **not GPS input**
and not a position feed into DJI. `USE FIX` copies the current visual
fix into the fields; `CLEAR` removes the offset.

### Mission queue

- **ADD TARGET** appends the lat/lon currently in the TARGET fields.
- **REMOVE** deletes the selected row, **CLEAR** empties the queue.
- Queue edits never start the aircraft; press **START MISSION** to fly.
- **START MISSION** flies waypoint 1, then auto-advances each time the
  stop radius is reached. **SKIP** jumps to the next point.
- When the last point is reached the mission completes and velocities go
  to zero.

## HTTP API

All endpoints are JSON. `GET` unless noted.

| endpoint | description |
|---|---|
| `GET /status` | fix + mode + control settings + calibration + mission + area |
| `GET /drive` | body-frame velocities for the Android app (poll ~10 Hz) |
| `GET /mission` | mission queue only |
| `GET /snapshot` | latest camera frame (JPEG, 204 before first frame) |
| `GET /health` | 200 when a fix exists, else 503 |
| `POST /command` | `{"action":"go","lat":..,"lon":..}` · `hold` · `stop` |
| `POST /calibrate` | `{"lat":..,"lon":..}` anchors the fix; `{"action":"clear"}` removes it |
| `POST /mission` | `{"action":"set","points":[[lat,lon],...]}` (edit queue, does not start) · `start` · `clear` · `skip` |
| `POST /settings` | any of `vmax` (m/s), `kp`, `stop_radius` (m), `hold_deadband` (m) |

### `/status`

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

`fix_valid` is the stale-fix watchdog: it turns false once
`age_s > position_timeout` (core config). Targeting commands are only
accepted inside `area`.

### `/drive`

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

Velocities are **body-frame**: forward is along the camera heading,
right is 90° clockwise from it. The Android runner maps them to DJI
virtual-stick pitch/roll. `blind: true` means an active command lost its
fix; the published velocities are already zero.

### Control loop

- `go`: horizontal error to target → `v = min(kp * dist, vmax)`,
  decomposed into body frame using the visual heading.
- Reaching `stop_radius` completes a single GO or advances the mission.
- `hold`: target is the fix at the moment HOLD is pressed. A deadband of
  `hold_deadband` stops velocity chatter.
- Any stale/invalid fix while `go` or `hold` is active → velocities
  zeroed, `blind: true`, and the drone station-keeps on DJI's own
  position hold until the visual fix returns.
- Optical flow only bridges short dropouts (`of_max_age`, default 3 s).
  After a longer blind gap the first full tile match must also be strong
  (`reacq_min_matches`, `reacq_min_confidence`) before the controller
  resumes; weak/jumpy matches are rejected.
