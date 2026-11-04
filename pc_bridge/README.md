> Shared visual core lives in `core/` — **git submodule** of
> [`BadministratorSrcn/Jam-Me`](https://github.com/BadministratorSrcn/Jam-Me):
>
> ```bash
> git clone --recurse-submodules https://github.com/BadministratorSrcn/Jam-Me-DJI.git
> # already cloned?  git submodule update --init
> # developer override without the submodule:  set JAMME_CORE=...Jam-Me
> ```

# Ground Bridge

Produces the visual fix for the Android app and exposes:

| endpoint | description |
|---|---|
| `GET /status` | JSON: `lat,lon,conf,matches,tile,src,heading,fix_valid,age_s` — poll 2-5 Hz |
| `GET /snapshot` | latest camera frame (JPEG) — HUD debug |
| `GET /health` | 200 when a fix exists |

## Sources

| `--source` | How | Needs |
|---|---|---|
| `sim` | same offline VirtualCamera flight as the Pi-repo `simulate.py` | a `sim_tiles_*` dir created there |
| `rtmp://host/stream` | `ffmpeg` rawvideo pipe → frames | ffmpeg binary on PATH (or `--ff-path`) |
| `file:PATH` | video loop (PC test / post-flightrecording analysis) | any H.264 file |

## Run offline demo (no drone)

```bash
python bridge_server.py --source sim --tiles-dir ..\sim_tiles_diyarbakir --port 8080
```
Open `http://127.0.0.1:8080/status` and `http://127.0.0.1:8080/snapshot`.

## Run with the drone

1. RC phone → Jam-Me-DJI app → RTMP out: `rtmp://<PC-hotspot-IP>:1935/live`
2. `python bridge_server.py --source rtmp://127.0.0.1:1935/live --port 8080`
   (RTMP *ingest* needs an RTMP *server*; two options):
   - simplest: run a tiny local server (e.g. `mediamtx` single binary) at 1935
   - or have ffmpeg pull `srt://`/`udp` when we switch output codec later
3. PC runs the visual core against the satellite tiles previously fetched
   for your mission area (see Pi repo `prepare_tiles` / `simulate.py`).

Wireless tip: PC hosts a hotspot; RC phone joins it → lowest latency path.
