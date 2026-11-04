#!/usr/bin/env python3
"""
=============================================================
JAM-ME-DJI — GROUND BRIDGE SERVER
=============================================================
Ingests the Mini 4 Pro camera stream and produces the visual
fix for the Android app:

  sources:  rtmp://... (ffmpeg required) | file:PATH.mp4 | sim
  pipeline: frames → TileLocalizer → PositionKalman → /status
  API:      GET /status   GET /snapshot   GET /health

The visual core is a git submodule of the Raspberry Pi repo
(Jam-Me) so both platforms literally share the same code:

    pc_bridge/core/  →  https://github.com/BadministratorSrcn/Jam-Me.git

Usage:
  python bridge_server.py --source sim [--tiles-dir sim_tiles_diyarbakir]
  python bridge_server.py --source rtmp://127.0.0.1:1935/live
  python bridge_server.py --source file:flight_recording.mp4

The bridge never assumes the drone is controllable against DJI
firmware — it exposes a trustworthy visual position + divergence
logging that the phone-side app overlays against DJI's own GPS.
=============================================================
"""

import argparse
import io
import json
import math
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

# --- visual core (git submodule of the Jam-Me Pi repo) ---
HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.environ.get("JAMME_CORE") or os.path.join(HERE, "core")
if not os.path.isdir(CORE) or not os.path.exists(os.path.join(CORE, "common.py")):
    print("ERROR: visual core missing.")
    print(f"Expected {CORE} containing common.py/main_tile.py")
    print("Fixes:  git submodule update --init        (preferred)")
    print("  or     set JAMME_CORE=C:\\path\\to\\Jam-Me      (developer override)")
    sys.exit(1)
sys.path.insert(0, CORE)
print(f"[bridge] visual core: {CORE}")

import simulate as sim_core          # noqa: E402  (from the Jam-Me submodule)
from common import Config, PositionKalman  # noqa: E402
import main_tile as flight           # noqa: E402

STATE = {
    "ts": 0.0,
    "last_update": -1e9,
    "fix_valid": False,
    "estimated": None,        # dict: lat/lon/conf/src/matches/heading/tile
    "frame": None,            # latest BGR frame for /snapshot
    "err_m": None,
    "tiles_loaded": 0,
}
STATE_LOCK = threading.Lock()
SIM_NOW = {"t": 0.0}
SIM_START_WALL = None          # set by SimSource while active

log = sim_core.log


# ============================================================
# FRAME SOURCES
# ============================================================
class RtmpSource:
    """ffmpeg → rawvideo(BGR24) stdout → frames (ffmpeg on PATH)."""

    def __init__(self, url, frame_w=640, frame_h=480, ffmpeg="ffmpeg"):
        self.frame_w, self.frame_h = frame_w, frame_h
        parts = url if url.startswith("file:") else url
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", url,
            "-vf", f"scale={frame_w}:{frame_h}:force_original_aspect_ratio=decrease,"
                   f"pad={frame_w}:{frame_h}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=frame_w * frame_h * 3)
        self.frame_size = frame_w * frame_h * 3

    def read(self):
        need = self.frame_size
        buf = b""
        while len(buf) < need:
            chunk = self.proc.stdout.read(need - len(buf))
            if not chunk:
                return None
            buf += chunk
        return np.frombuffer(buf, np.uint8).reshape(self.frame_h, self.frame_w, 3)


class FileSource:
    def __init__(self, path, fps=15.0):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open video: {path}")
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.need_resize = (self.w, self.h) != (640, 480)
        self.fps = fps if fps and fps > 0 else (self.cap.get(cv2.CAP_PROP_FPS) or 15.0)

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_MSEC, 0)   # loop
            ok, frame = self.cap.read()
            if not ok:
                return None
        return cv2.resize(frame, (640, 480)) if self.need_resize else frame


class SimSource:
    """Offline demo: the same VirtualCamera + flight plan as simulate.py."""

    def __init__(self, tiles_dir, origin, duration=48.0, speed=12.0, dt=0.25):
        self.cam = sim_core.VirtualCamera(
            os.path.join(tiles_dir, "tile_config.json"), tiles_dir)
        self.plan = sim_core.FlightPlan(origin_m=(350.0, 750.0), speed=speed)
        self.plan.set_segments(duration)
        self.duration, self.dt, self.i = duration, dt, 0
        self.start_wall = time.time()

    def read(self):
        t = self.i * self.dt
        if t > self.duration + 2:
            self.i = 0
            t = 0
        self.i += 1
        global SIM_START_WALL
        SIM_START_WALL = self.start_wall
        SIM_NOW["t"] = self.start_wall + t
        e, n, imp = self.plan.position_at(t)
        lat = self.cam.origin_lat - sim_core.deg_lat(n)
        lon = self.cam.origin_lon + e * sim_core.lon_per_meter(self.cam.origin_lat)
        return self.cam.render(lat, lon, 0.0, imp)


# ============================================================
# WORKER — frames → localizer → kalman → STATE
# ============================================================
def worker(cfg, src, exit_evt):
    tile_cache = flight.TileCache(cfg)
    localizer = flight.TileLocalizer(tile_cache, cfg)
    kalman = PositionKalman(cfg.process_noise, cfg.measurement_noise)
    init = flight.InitialLocalizer(tile_cache, cfg)

    with STATE_LOCK:
        STATE["tiles_loaded"] = len(tile_cache.tile_meta)

    # first frame → initial localization
    first = None
    for _ in range(10):
        first = src.read()
        if first is not None:
            break
    if first is None:
        log.error("no initial frame available from source")
        return
    ok = init.find_initial_position(first)
    log.info(f"initial tile: {ok}" if ok else "initial localization FAILED")

    while not exit_evt.is_set():
        frame = src.read()
        if frame is None:
            exit_evt.wait(0.1)
            continue
        res = localizer.match_frame(frame)
        now = SIM_NOW["t"] if isinstance(src, SimSource) else time.time()

        est = None
        if res.found:
            k_lat, k_lon = kalman.update(res.lat, res.lon, res.confidence)
            est = {
                "lat": k_lat, "lon": k_lon,
                "conf": res.confidence, "matches": res.match_count,
                "heading": res.heading, "tile": res.tile_key,
                "src": ("optical_flow" if res.tile_key == "optical_flow" else "visual")
            }
        with STATE_LOCK:
            STATE["frame"] = frame.copy()
            if est:
                STATE["estimated"] = est
                STATE["ts"] = now
                STATE["last_update"] = now
            STATE["fix_valid"] = bool(
                est is not None and
                (now - STATE.get("last_update", -1e9)) <= cfg.position_timeout)


# ============================================================
# HTTP API
# ============================================================
class ApiHTTP(BaseHTTPRequestHandler):
    server_version = "JamMeDJI-Bridge/0.1"

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        with STATE_LOCK:
            state = {
                "ts": STATE["ts"],
                "age_s": round(SIM_NOW["t"] - STATE["ts"], 2)
                         if isinstance(SIM_NOW.get("t"), (int, float)) and STATE["ts"] else None,
                "estimated": STATE["estimated"],
                "fix_valid": STATE["fix_valid"],
                "tiles_loaded": STATE["tiles_loaded"],
            }
        if path == "/status":
            body = json.dumps(state).encode()
            self._send(200, body)

        elif path == "/snapshot":
            with STATE_LOCK:
                frame = STATE.get("frame")
            if frame is None:
                self._send(204, b"", "text/plain")
                return
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok:
                self._send(500, b"", "text/plain")
                return
            self._send(200, jpg.tobytes(), "image/jpeg")

        elif path == "/health":
            ok = STATE["estimated"] is not None
            self._send(200 if ok else 503,
                       json.dumps({"ok": ok, "backend": "bridge"}).encode(),
                       "application/json")
        else:
            self._send(404, b'{"error":"not found"}')


# ============================================================
def main():
    p = argparse.ArgumentParser(description="Jam-Me-DJI ground bridge")
    p.add_argument("--source", required=True,
                   help="rtmp://URL | file:PATH | sim")
    p.add_argument("--tiles-dir", default="sim_tiles_diyarbakir")
    p.add_argument("--origin", default=None,
                   help="sim mode grid corner lat,lon (from tile_config by default)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--ff-path", default="ffmpeg", help="ffmpeg binary path")
    args = p.parse_args()

    cfg = Config(tile_dir=args.tiles_dir)

    if args.source == "sim":
        if not os.path.exists(os.path.join(args.tiles_dir, "tile_config.json")):
            print(f"tiles not found at {args.tiles_dir} — run simulate.py once for that area")
            sys.exit(1)
        src = SimSource(args.tiles_dir, args.origin)
    elif args.source.startswith("rtmp"):
        src = RtmpSource(args.source, ffmpeg=args.ff_path)
    elif args.source.startswith("file:"):
        src = FileSource(args.source[5:])
    else:
        print("unknown --source"); sys.exit(1)

    exit_evt = threading.Event()
    threading.Thread(target=worker, args=(cfg, src, exit_evt), daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), ApiHTTP)
    print("=" * 64)
    print(f"Jam-Me-DJI bridge listening on http://0.0.0.0:{args.port}")
    print("  GET /status    → visual fix JSON (poll 2-5 Hz)")
    print("  GET /snapshot  → latest frame JPEG")
    print("  GET /health")
    print("=" * 64)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        exit_evt.set()
        srv.server_close()


if __name__ == "__main__":
    main()
