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

M_LAT = 111320.0          # meters per degree latitude

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
AREA_BOX = None                # ((lat_min, lat_max), (lon_min, lon_max))

# --- control command state (PC GUI -> app VirtualStick) ---
CMD = {
    "id": 0,
    "mode": "idle",            # idle | go | hold | complete
    "target": None,            # (lat, lon) in degrees
    "v_forward": 0.0,          # m/s along camera heading
    "v_right": 0.0,            # m/s to the right of heading
    "dist_m": None,
    "bearing_deg": None,
    "blind": False,            # fix invalid while a command is active
}
CMD_LOCK = threading.Lock()

# bridge-side control defaults (overridable via CLI flags)
CTRL = {"vmax": 2.5, "kp": 0.6, "stop_radius": 1.5}   # m/s, gain, m


def drive_command():
    """GET /drive payload for the Android VirtualStick runner."""
    with CMD_LOCK:
        cmd = dict(CMD)
    return {
        "id": cmd["id"],
        "mode": cmd["mode"],
        "target": cmd["target"],
        "v_forward": round(cmd["v_forward"], 3),
        "v_right": round(cmd["v_right"], 3),
        "dist_m": cmd["dist_m"],
        "bearing_deg": cmd["bearing_deg"],
        "blind": cmd["blind"],
        "note": "velocities are body-frame (forward=along heading); "
                "vmax=%s; blind fix => zero" % CTRL["vmax"],
    }


def update_command(mode_="idle", target=None):
    """PC-side command entry (POST /command)."""
    with CMD_LOCK:
        CMD["id"] += 1
        CMD["mode"] = mode_
        CMD["target"] = target
        CMD["v_forward"] = CMD["v_right"] = 0.0
        CMD["blind"] = False
        if mode_ != "go":
            CMD["dist_m"] = CMD["bearing_deg"] = None


def run_controller(est, heading_deg):
    """
    The GPS-free position servo used for BOTH 'go' and 'hold'.

    Feedback comes exclusively from the visual fix (tile matching +
    Kalman). If the fix is stale/blind, velocities are forced to zero —
    the aircraft holds via VirtualStick zeros instead of flying blind.
    """
    if est is None:
        with CMD_LOCK:
            if CMD["mode"] in ("go", "hold"):
                CMD["blind"] = True
                CMD["v_forward"] = CMD["v_right"] = 0.0
        return

    with CMD_LOCK:
        if CMD["mode"] not in ("go", "hold") or CMD["target"] is None:
            return
        t_lat, t_lon = CMD["target"]
        # error in meters (north, east)
        dn = (t_lat - est["lat"]) * M_LAT
        de = (t_lon - est["lon"]) * M_LAT * math.cos(math.radians(est["lat"]))
        dist = math.hypot(de, dn)
        CMD["blind"] = False
        CMD["dist_m"] = round(dist, 2)
        CMD["bearing_deg"] = round(math.degrees(math.atan2(de, dn)) % 360, 1)

        if CMD["mode"] == "go" and dist <= CTRL["stop_radius"]:
            CMD["mode"] = "complete"
            CMD["v_forward"] = CMD["v_right"] = 0.0
            return

        v = min(CTRL["kp"] * dist, CTRL["vmax"])
        th = math.radians(heading_deg)
        f_comp = de * math.sin(th) + dn * math.cos(th)
        r_comp = de * math.cos(th) - dn * math.sin(th)
        d = max(dist, 1e-6)
        CMD["v_forward"] = round(v * f_comp / d, 3)
        CMD["v_right"] = round(v * r_comp / d, 3)

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
    """
    Offline closed-loop drone: flies the scripted plan only in the first
    seconds; afterwards it is driven EXCLUSIVELY by the bridge's visual
    servo (`step()` integrates commanded body velocities). With
    speed=0 and a flat plan the sim is a pure "GPS-less control sandbox"
    — exactly what the PC GUI + /drive loop will exercise in production.
    """

    def __init__(self, tiles_dir, duration=48.0, speed=0.0, dt=0.25):
        self.cam = sim_core.VirtualCamera(
            os.path.join(tiles_dir, "tile_config.json"), tiles_dir)
        self.plan = sim_core.FlightPlan(origin_m=(350.0, 750.0), speed=speed)
        self.plan.set_segments(duration)
        self.duration, self.dt, self.i = duration, dt, 0
        self.start_wall = time.time()
        self.origin_m = [350.0, 750.0]        # mutable (east, north)

    def read(self):
        # time advances by dt per read (process as fast as possible)
        e, n, imp = self.plan.position_at(self.i * self.dt)
        self.i += 1
        global SIM_START_WALL
        SIM_START_WALL = self.start_wall
        SIM_NOW["t"] = self.start_wall + self.i * self.dt
        lat = self.cam.origin_lat - sim_core.deg_lat(n)
        lon = self.cam.origin_lon + e * sim_core.lon_per_meter(self.cam.origin_lat)
        return self.cam.render(lat, lon, 0.0, imp)

    def step(self, v_forward, v_right, dt):
        """
        Integrate commanded body velocities (m/s) into the sim drone's
        position. NOTE the flight-plan coordinate semantics:
        origin_m = [meter EAST, meter SOUTH] of the grid corner (image
        convention, matching sim_core.FlightPlan/virtual camera y axis).
        Camera heading sim = 0 ⇒ forward points NORTH ⇒ south-positive
        y DECREASES.
        """
        if dt <= 0:
            return
        self.origin_m[0] += v_right * dt
        self.origin_m[1] -= v_forward * dt
        # repaint the flight plan origin so position_at() mirrors it
        self.plan.origin = (self.origin_m[0], self.origin_m[1])


# ============================================================
# WORKER — frames → localizer → kalman → STATE
# ============================================================
def worker(cfg, src, exit_evt, dt=0.25):
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
            # GPS-free position servo -> body velocities for VirtualStick
            run_controller(est, res.heading)
        else:
            # fix lost: zero the servos (safe hold in place)
            run_controller(None, None)
        # sim-only: apply commanded velocities to the virtual drone
        if isinstance(src, SimSource):
            with CMD_LOCK:
                vf, vr = CMD["v_forward"], CMD["v_right"]
            src.step(vf, vr, dt)
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
                "age_s": round(time.time() - STATE["ts"], 2) if STATE["ts"] else None,
                "estimated": STATE["estimated"],
                "fix_valid": STATE["fix_valid"],
                "tiles_loaded": STATE["tiles_loaded"],
            }
        if path == "/status":
            body = json.dumps(state).encode()
            self._send(200, body)

        elif path == "/drive":
            drive = drive_command()
            body = json.dumps(drive).encode()
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

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/command":
            self._send(404, b'{"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": f"bad json: {e}"}).encode())
            return

        action = str(req.get("action", "")).lower()
        if action == "go":
            t_lat, t_lon = req.get("lat"), req.get("lon")
            if t_lat is None or t_lon is None:
                self._send(400, json.dumps({"ok": False, "error": "go needs lat/lon"}).encode())
                return
            if AREA_BOX is None:
                self._send(500, json.dumps({"ok": False, "error": "no tile area loaded"}).encode())
                return
            lat0, lat1 = AREA_BOX[0]; lon0, lon1 = AREA_BOX[1]
            if not (lat0 <= t_lat <= lat1 and lon0 <= t_lon <= lon1):
                self._send(400, json.dumps({
                    "ok": False,
                    "error": "target outside the loaded tile area",
                    "area": {"lat": [lat0, lat1], "lon": [lon0, lon1]}}).encode())
                return
            update_command("go", (float(t_lat), float(t_lon)))
            self._send(200, json.dumps({"ok": True, "mode": "go", "target": [t_lat, t_lon],
                                        **CTRL}).encode())
        elif action == "hold":
            est = STATE["estimated"]
            if est is None:
                self._send(409, json.dumps({"ok": False, "error": "no visual fix yet to hold"}).encode())
                return
            update_command("hold", (est["lat"], est["lon"]))
            self._send(200, json.dumps({"ok": True, "mode": "hold", "target": [est["lat"], est["lon"]]}).encode())
        elif action == "stop":
            update_command("idle")
            self._send(200, json.dumps({"ok": True, "mode": "idle"}).encode())
        else:
            self._send(400, json.dumps({"ok": False, "error": "unknown action (go|hold|stop)"}).encode())


# ============================================================
def main():
    global AREA_BOX
    p = argparse.ArgumentParser(description="Jam-Me-DJI ground bridge")
    p.add_argument("--source", required=True,
                   help="rtmp://URL | file:PATH | sim")
    p.add_argument("--tiles-dir", default="sim_tiles_diyarbakir")
    p.add_argument("--origin", default=None,
                   help="sim mode grid corner lat,lon (from tile_config by default)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--ff-path", default="ffmpeg", help="ffmpeg binary path")
    p.add_argument("--vmax", type=float, default=CTRL["vmax"], help="max commanded speed m/s")
    p.add_argument("--kp", type=float, default=CTRL["kp"])
    p.add_argument("--stop-radius", type=float, default=CTRL["stop_radius"])
    p.add_argument("--sim-speed", type=float, default=0.0,
                   help="sim mode wind (auto flight) speed m/s; 0 = pure control sandbox")
    p.add_argument("--sim-duration", type=float, default=30.0)
    p.add_argument("--sim-dt", type=float, default=0.25)
    args = p.parse_args()

    CTRL.update(vmax=args.vmax, kp=args.kp, stop_radius=args.stop_radius)

    cfg = Config(tile_dir=args.tiles_dir)

    # area bounds for /command validation
    tile_cfg_path = os.path.join(args.tiles_dir, "tile_config.json")
    if not os.path.exists(tile_cfg_path):
        print(f"tiles not found: {tile_cfg_path} — run simulate.py first")
        sys.exit(1)
    with open(tile_cfg_path) as f:
        tc = json.load(f)
    area = tc["area"]
    AREA_BOX = (
        (area["bottom_right"]["lat"], area["top_left"]["lat"]),
        (area["top_left"]["lon"], area["bottom_right"]["lon"]),
    )

    if args.source == "sim":
        src = SimSource(args.tiles_dir, duration=args.sim_duration,
                        speed=args.sim_speed, dt=args.sim_dt)
    elif args.source.startswith("rtmp"):
        src = RtmpSource(args.source, ffmpeg=args.ff_path)
    elif args.source.startswith("file:"):
        src = FileSource(args.source[5:])
    else:
        print("unknown --source"); sys.exit(1)

    exit_evt = threading.Event()
    threading.Thread(target=worker, args=(cfg, src, exit_evt, getattr(src, "dt", 0.0)), daemon=True).start()

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
