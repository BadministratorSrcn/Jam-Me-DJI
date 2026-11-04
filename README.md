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
│ Android (RC-2/N-2 phone)│   WiFi  │ Ground PC                      │
│  Jam-Me-DJI app         │         │  bridge_server.py              │
│  ├ MSDK V5 (Mini 4 Pro) │◄────────┤  ├ RTMP ingest (ffmpeg)        │
│  ├ LiveStreamManager ───┼─RTMP───►│  ├ visual core (submodule):    │
│  │    RTMP out          │         │  │  TileLocalizer+Kalman        │
│  ├ telemetry recorder   │         │  └ HTTP API /status /snapshot  │
│  ├ VisualPosClient ─────┼──HTTP──►│                                │
│  └ HUD overlay + CSV log│         └────────────────────────────────┘
│     (DJI pos vs visual) │
└─────────────────────────┘
```

Ground PC runs against pre-loaded satellite tiles (`sim_tiles_*` /
`tiles/` from the Pi repo flow — same format).

**Honest mission profile on DJI:** with GPS present → normal MSDK
missions/waypoints; when GPS degrades → DJI firmware rules apply (drone
holds/RTHs per its own vision system) while our stack **continues to
report the true visual position, logs the divergence, and geotags
evidence** — it cannot override DJI's flight control. Direct visual
drive would be the Mavic-3E/Matriceenterprise + PSDK/MAVSDK story, not
Mini 4 Pro.

---

## Components

| Path | What it is |
|---|---|
| `pc_bridge/` | Ground PC: RTMP ingest → visual core → HTTP API (Python; reuses `common.py`/`main_tile.py` via git submodule) |
| `app/` | Android Kotlin MSDK V5 app skeleton (registration, RTMP stream start, telemetry recorder, visual-position client & HUD) |
| `docs/ARCHITECTURE.md` | Data flows, message schema, DJI constraints detail, phase-2 (Kotlin port) plan |

## Quick start (PC part — works today, no drone needed)

```bash
git clonehttps://github.com/BadministratorSrcn/Jam-Me.git pc_bridge/core
# (placeholder: git submodule init once repo is published)
python -m pip install -r pc_bridge/requirements.txt

# offline demo — bridge consumes the SIMULATED flight (same pipeline!)
python pc_bridge/bridge_server.py --source sim \
    --tiles-dir pc_bridge/../sim_tiles_diyarbakir --port 8080
# then open http://127.0.0.1:8080/status  and /snapshot
```

Real drone: the RTMP URL from the Android app (`rtmp://<pc-ip>:8081/live`)
is ingested automatically:

```bash
python pc_bridge/bridge_server.py --source rtmp://127.0.0.1:1935/live --port 8080
# ffmpeg required on PATH (or --ffmpeg path)
```

## Quick start (Android part)

1. **DJI Developer Console** (developer.dji.com) → create an MSDK V5 app
   → App Key (you must do this yourself once; free).
2. Android Studio → open `app/` → paste your App Key into
   `app/src/main/res/values/apikey.xml` placeholder.
3. Follow MSDK V5 official sample for local AAR dependencies or repo
   snippets (see `docs/ARCHITECTURE.md` → "Build notes").
4. Connect RC-N2/N3 + Mini 4 Pro, run the app; it registers, connects,
   starts RTMP out and polls ground PC for the visual fix; HUD shows
   DJI-GPS vs visual-GPS divergence.

## Status / roadmap

- [x] PC bridge (RTMP ingest + visual core + HTTP API) — works offline & online
- [x] Android skeleton (registration/live stream/telemetry/HUD)
- [ ] WaypointMission runner (GPS-mode missions via MSDK)
- [ ] EXIF geotagging of recorded media with visual fix
- [ ] Phase 2: Kotlin port of visual core (when DJI fixes Mini 4 Pro `VideoFeed`)
- [ ] Optional: enterprise aircraft (Mavic 3E etc.) branch with PSDK position feed

## License

MIT — see LICENSE (same as Jam-Me).
