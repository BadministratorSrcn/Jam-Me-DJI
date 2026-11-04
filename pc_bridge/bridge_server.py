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
    "wall_update": 0.0,       # wall-clock time of the last visual fix
    "fix_timeout": 1.0,       # copied from Config.position_timeout
    "fix_valid": False,
    "estimated": None,        # dict: lat/lon/conf/src/matches/heading/tile
    "frame": None,            # latest BGR frame for /snapshot
    "err_m": None,
    "tiles_loaded": 0,
    "dji": None,              # reference-only DJI GPS (never used for control)
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
CTRL = {"vmax": 2.5, "kp": 0.6, "stop_radius": 1.5,
        "hold_deadband": 0.35}                        # m/s, gain, m, m

# manual position correction (entered from PC GUI): offset added to the
# visual fix. Lets the operator anchor the estimate to a measured real
# coordinate without breaking the closed loop (visual fixes continue,
# shifted by the given amount).
CAL_OFFSET = {"dlat": 0.0, "dlon": 0.0, "enabled": False}

# multi-point mission queue (PC GUI builds it)
MISSION = {"points": [], "idx": -1, "active": False}


def drive_command():
    """GET /drive payload for the Android VirtualStick runner."""
    with CMD_LOCK:
        cmd = dict(CMD)
    # Wall-clock backstop: if the frame pipeline stalls (RTMP freeze, ffmpeg
    # hang, camera unplugged), the worker may block and never refresh
    # fix_valid. Never publish a moving command after the visual fix expires.
    with STATE_LOCK:
        wall_age = time.time() - STATE.get("wall_update", 0.0)
        stale = wall_age > STATE.get("fix_timeout", 1.0)
    if stale and cmd["mode"] in ("go", "hold"):
        cmd["v_forward"] = 0.0
        cmd["v_right"] = 0.0
        cmd["blind"] = True
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


def in_area(lat, lon):
    """True when a coordinate lies inside the loaded tile rectangle."""
    if AREA_BOX is None:
        return False
    lat0, lat1 = AREA_BOX[0]
    lon0, lon1 = AREA_BOX[1]
    return lat0 <= lat <= lat1 and lon0 <= lon <= lon1


def mission_payload():
    with CMD_LOCK:
        m = dict(MISSION)
        m["points"] = [list(p) for p in MISSION["points"]]
        current = (MISSION["points"][MISSION["idx"]]
                   if 0 <= MISSION["idx"] < len(MISSION["points"]) else None)
    m["current"] = list(current) if current else None
    m["count"] = len(m["points"])
    return m


def apply_calibration(raw_lat, raw_lon):
    """Shift a raw visual fix by the operator-entered anchor offset."""
    with CMD_LOCK:
        return raw_lat + CAL_OFFSET["dlat"], raw_lon + CAL_OFFSET["dlon"]


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
            # mission auto-advance: fly the queue waypoint by waypoint
            if MISSION["active"] and MISSION["idx"] + 1 < len(MISSION["points"]):
                MISSION["idx"] += 1
                CMD["target"] = tuple(MISSION["points"][MISSION["idx"]])
                CMD["mode"] = "go"
                CMD["v_forward"] = CMD["v_right"] = 0.0
                log.info("Mission waypoint %d/%d reached → next target"
                         % (MISSION["idx"], len(MISSION["points"])))
                return
            if MISSION["active"]:
                MISSION["active"] = False
                MISSION["idx"] = -1
                log.info("Mission complete")
            CMD["mode"] = "complete"
            CMD["v_forward"] = CMD["v_right"] = 0.0
            return

        if CMD["mode"] == "hold" and dist <= CTRL.get("hold_deadband", 0.35):
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
    """ffmpeg → rawvideo(BGR24) stdout → frames (ffmpeg on PATH).

    The publisher (the Android app) may start after the bridge, and it may
    drop off mid-flight. ffmpeg is therefore respawned automatically; a
    missing stream is a normal waiting state, not a fatal error.
    """

    def __init__(self, url, frame_w=640, frame_h=480, ffmpeg="ffmpeg"):
        self.url = url
        self.frame_w, self.frame_h = frame_w, frame_h
        self.ffmpeg = ffmpeg
        self.frame_size = frame_w * frame_h * 3
        self.proc = None
        self._spawn()

    def _spawn(self):
        if self.url.startswith("rtsp"):
            proto = ["-rtsp_transport", "tcp", "-timeout", "5000000"]
        else:
            proto = ["-rw_timeout", "5000000"]
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
        ] + proto + [
            "-i", self.url,
            "-vf", f"scale={self.frame_w}:{self.frame_h}:force_original_aspect_ratio=decrease,"
                   f"pad={self.frame_w}:{self.frame_h}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     bufsize=self.frame_size)

    def read(self):
        need = self.frame_size
        buf = b""
        while len(buf) < need:
            chunk = self.proc.stdout.read(need - len(buf))
            if not chunk:
                # Stream not published yet, or it just ended. Restart ffmpeg
                # after a short pause so the bridge can wait indefinitely.
                try:
                    self.proc.kill()
                except Exception:
                    pass
                time.sleep(1.0)
                self._spawn()
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

    # first frame → initial localization. The publisher (phone RTMP) may not
    # be live yet when the bridge starts, so wait for it instead of exiting.
    first = None
    last_wait_log = 0.0
    while not exit_evt.is_set() and first is None:
        first = src.read()
        if first is None:
            now_wait = time.time()
            if now_wait - last_wait_log > 5.0:
                log.info("waiting for first video frame from source...")
                last_wait_log = now_wait
            exit_evt.wait(0.5)
    if first is None:
        log.error("no initial frame available from source")
        return
    ok = init.find_initial_position(first)
    log.info(f"initial tile: {ok}" if ok else "initial localization FAILED")
    last_init_try = time.time()

    # Decouple video delivery from visual matching: the grabber keeps the
    # latest frame flowing to the GUI at source rate, while the worker
    # localizes at its own (slower) rate. The scripted simulator keeps its
    # single-threaded read/step loop so its physics stays deterministic.
    use_grabber = not isinstance(src, SimSource)
    latest = {"frame": None, "seq": 0}

    def _grab():
        while not exit_evt.is_set():
            f = src.read()
            if f is None:
                with STATE_LOCK:
                    STATE["fix_valid"] = False
                run_controller(None, None)
                exit_evt.wait(0.05)
                continue
            with STATE_LOCK:
                STATE["frame"] = f.copy()
            latest["frame"] = f
            latest["seq"] += 1

    if use_grabber:
        threading.Thread(target=_grab, daemon=True).start()

    last_seq = -1
    last_fix_clock = -1e9
    while not exit_evt.is_set():
        if use_grabber:
            frame = latest["frame"]
            seq = latest["seq"]
            if frame is None or seq == last_seq:
                exit_evt.wait(0.02)
                continue
            last_seq = seq
        else:
            frame = src.read()
            if frame is None:
                with STATE_LOCK:
                    STATE["fix_valid"] = False
                run_controller(None, None)
                exit_evt.wait(0.1)
                continue

        # The first frame may be a boot screen or a forward-facing camera
        # before the gimbal is pointed down. Keep retrying the global initial
        # search until a tile is matched; without this the localizer has no
        # active tiles and can never acquire a fix.
        if not ok:
            if time.time() - last_init_try >= 2.0:
                last_init_try = time.time()
                ok = init.find_initial_position(frame)
                if ok:
                    log.info("initial localization acquired on retry")
            if not ok:
                with STATE_LOCK:
                    STATE["fix_valid"] = False
                run_controller(None, None)
                continue

        res = localizer.match_frame(frame)
        now = SIM_NOW["t"] if isinstance(src, SimSource) else time.time()

        est = None
        if res.found:
            gap = now - last_fix_clock > cfg.position_timeout
            # Reacquisition gate: after a blind gap, demand a strong full
            # tile match. A weak/jumpy match must not wake the controller.
            if gap and (res.tile_key == "optical_flow" or
                        res.match_count < cfg.reacq_min_matches or
                        res.confidence < cfg.reacq_min_confidence):
                log.warning("Reacquisition rejected (matches=%d conf=%.2f)"
                            % (res.match_count, res.confidence))
                res.found = False
            else:
                if gap:
                    # Re-seed the filter with the new match; the old state
                    # is stale and would drag the output for seconds.
                    kalman.reset(res.lat, res.lon, now=now)
                    k_lat, k_lon = res.lat, res.lon
                else:
                    k_lat, k_lon = kalman.update(res.lat, res.lon, res.confidence,
                                                 now=now)
                last_fix_clock = now
                k_lat, k_lon = apply_calibration(k_lat, k_lon)
                est = {
                    "lat": k_lat, "lon": k_lon,
                    "conf": res.confidence, "matches": res.match_count,
                    "heading": res.heading, "tile": res.tile_key,
                    "src": ("optical_flow" if res.tile_key == "optical_flow" else "visual")
                }
                # GPS-free position servo -> body velocities for VirtualStick
                run_controller(est, res.heading)
        if est is None:
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
                STATE["wall_update"] = time.time()
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

    def _area_json(self):
        if AREA_BOX is None:
            return None
        return {"lat": list(AREA_BOX[0]), "lon": list(AREA_BOX[1])}

    def _google_map_html(self):
        """Serve the Google Maps JavaScript page with the key injected."""
        path = os.path.join(HERE, "google_map.html")
        with open(path, "r", encoding="utf-8") as f:
            doc = f.read()
        key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
        if AREA_BOX:
            lat = (AREA_BOX[0][0] + AREA_BOX[0][1]) / 2.0
            lon = (AREA_BOX[1][0] + AREA_BOX[1][1]) / 2.0
        else:
            lat, lon = 37.9073, 40.2389
        return (doc
                .replace("__GOOGLE_MAPS_API_KEY__", key)
                .replace("__KEY_PRESENT__", "true" if key else "false")
                .replace("__CENTER_LAT__", f"{lat:.7f}")
                .replace("__CENTER_LON__", f"{lon:.7f}")
                .replace("__ZOOM__", "15"))

    def _stream_mjpeg(self):
        """Continuous multipart JPEG stream for the local GUI (low latency)."""
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                with STATE_LOCK:
                    frame = STATE.get("frame")
                if frame is None:
                    time.sleep(0.05)
                    continue
                ok, jpg = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ok:
                    continue
                data = jpg.tobytes()
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(
                    b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n")
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/stream.mjpg":
            self._stream_mjpeg()
            return
        if path == "/map":
            self._send(200, self._google_map_html().encode("utf-8"), "text/html")
            return
        with STATE_LOCK:
            wall_age = time.time() - STATE.get("wall_update", 0.0)
            fresh = wall_age <= STATE.get("fix_timeout", 1.0)
            dji = STATE.get("dji")
            state = {
                "ts": STATE["ts"],
                "age_s": (round(max(0.0, time.time() - STATE["ts"]), 2)
                          if STATE["ts"] else None),
                "fix_age_s": round(wall_age, 2),
                "estimated": STATE["estimated"],
                "fix_valid": STATE["fix_valid"] and fresh,
                "tiles_loaded": STATE["tiles_loaded"],
                "dji": dji,
                "dji_age_s": (round(time.time() - dji["ts"], 2) if dji else None),
            }
        if path == "/status":
            state["ctrl"] = dict(CTRL)
            state["cal_offset"] = dict(CAL_OFFSET)
            state["mission"] = mission_payload()
            state["area"] = self._area_json()
            state["mode"] = drive_command()["mode"]
            self._send(200, json.dumps(state).encode())

        elif path == "/drive":
            body = json.dumps(drive_command()).encode()
            self._send(200, body)

        elif path == "/mission":
            self._send(200, json.dumps(mission_payload()).encode())

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
            with STATE_LOCK:
                ok = STATE["estimated"] is not None
                age = (time.time() - STATE["ts"]) if STATE["ts"] else None
            self._send(200 if ok else 503,
                       json.dumps({"ok": ok, "backend": "bridge",
                                   "fix_age_s": round(age, 2) if age else None}).encode(),
                       "application/json")
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) or b"{}"
            req = json.loads(body)
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": f"bad json: {e}"}).encode())
            return

        if path == "/command":
            self.handle_command(req)
        elif path == "/calibrate":
            self.handle_calibrate(req)
        elif path == "/mission":
            self.handle_mission(req)
        elif path == "/settings":
            self.handle_settings(req)
        elif path == "/telemetry":
            self.handle_telemetry(req)
        else:
            self._send(404, b'{"error":"not found"}')

    def handle_telemetry(self, req):
        """Reference-only DJI GPS from the phone; never feeds the controller."""
        try:
            lat = float(req.get("lat"))
            lon = float(req.get("lon"))
        except (TypeError, ValueError):
            self._send(400, json.dumps({"ok": False, "error": "lat/lon required"}).encode())
            return
        with STATE_LOCK:
            STATE["dji"] = {
                "lat": lat, "lon": lon,
                "heading": req.get("heading"),
                "alt": req.get("alt"),
                "sats": req.get("sats"),
                "ts": time.time(),
            }
        self._send(200, b'{"ok":true}')

    # ---- POST endpoints -----------------------------------------
    def handle_command(self, req):
        action = str(req.get("action", "")).lower()
        if action == "go":
            t_lat, t_lon = req.get("lat"), req.get("lon")
            if t_lat is None or t_lon is None:
                self._send(400, json.dumps({"ok": False, "error": "go needs lat/lon"}).encode())
                return
            try:
                t_lat, t_lon = float(t_lat), float(t_lon)
            except (TypeError, ValueError):
                self._send(400, json.dumps({"ok": False, "error": "lat/lon must be numbers"}).encode())
                return
            if AREA_BOX is None:
                self._send(500, json.dumps({"ok": False, "error": "no tile area loaded"}).encode())
                return
            if not in_area(t_lat, t_lon):
                self._send(400, json.dumps({
                    "ok": False,
                    "error": "target outside the loaded tile area",
                    "area": self._area_json()}).encode())
                return
            with CMD_LOCK:
                MISSION["active"] = False
                MISSION["idx"] = -1
            update_command("go", (t_lat, t_lon))
            self._send(200, json.dumps({"ok": True, "mode": "go", "target": [t_lat, t_lon],
                                        "ctrl": dict(CTRL)}).encode())
        elif action == "hold":
            with STATE_LOCK:
                est = STATE["estimated"]
            if est is None:
                self._send(409, json.dumps({"ok": False, "error": "no visual fix yet to hold"}).encode())
                return
            with CMD_LOCK:
                MISSION["active"] = False
                MISSION["idx"] = -1
            update_command("hold", (est["lat"], est["lon"]))
            self._send(200, json.dumps({"ok": True, "mode": "hold",
                                        "target": [est["lat"], est["lon"]]}).encode())
        elif action == "stop":
            with CMD_LOCK:
                MISSION["active"] = False
                MISSION["idx"] = -1
            update_command("idle")
            self._send(200, json.dumps({"ok": True, "mode": "idle"}).encode())
        else:
            self._send(400, json.dumps({"ok": False,
                                        "error": "unknown action (go|hold|stop)"}).encode())

    def handle_calibrate(self, req):
        """
        POST /calibrate {"lat":.., "lon":..}
        The operator types the drone's REAL current coordinate (read from a
        map, a hand-held GPS, or the DJI app when it has a fix). We compute
        the difference against the raw visual fix and shift every future
        fix by it. The closed loop keeps running on tile matching only —
        this is a one-time anchor correction, not GPS input.
        """
        action = str(req.get("action", "set")).lower()
        with CMD_LOCK:
            old = dict(CAL_OFFSET)
        if action in ("clear", "reset"):
            with CMD_LOCK:
                CAL_OFFSET.update(dlat=0.0, dlon=0.0, enabled=False)
            self._send(200, json.dumps({"ok": True, "cal_offset": dict(CAL_OFFSET)}).encode())
            return

        with STATE_LOCK:
            est = STATE["estimated"]
        if est is None:
            self._send(409, json.dumps({"ok": False,
                                        "error": "no visual fix yet to calibrate"}).encode())
            return
        try:
            lat = float(req["lat"]); lon = float(req["lon"])
        except (KeyError, TypeError, ValueError):
            self._send(400, json.dumps({"ok": False, "error": "need numeric lat/lon"}).encode())
            return
        if not in_area(lat, lon):
            self._send(400, json.dumps({"ok": False,
                                        "error": "coordinate outside the loaded tile area",
                                        "area": self._area_json()}).encode())
            return
        raw_lat = est["lat"] - old["dlat"]
        raw_lon = est["lon"] - old["dlon"]
        dlat, dlon = lat - raw_lat, lon - raw_lon
        with CMD_LOCK:
            CAL_OFFSET.update(dlat=dlat, dlon=dlon, enabled=True)
        dm = math.hypot(dlat * M_LAT,
                        dlon * M_LAT * math.cos(math.radians(lat)))
        self._send(200, json.dumps({
            "ok": True, "mode": "calibrated",
            "cal_offset": {"dlat": dlat, "dlon": dlon, "enabled": True},
            "offset_m": round(dm, 2),
            "current": [lat, lon],
        }).encode())

    def _parse_mission_points(self, pts):
        """Validate [[lat, lon], ...]; returns (clean, error-string)."""
        if not isinstance(pts, list):
            return None, "points must be a list"
        clean = []
        for i, p in enumerate(pts):
            try:
                p_lat, p_lon = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                return None, f"point {i} needs [lat, lon]"
            if not in_area(p_lat, p_lon):
                return None, f"point {i} outside the loaded tile area"
            clean.append((p_lat, p_lon))
        return clean, None

    def handle_mission(self, req):
        action = str(req.get("action", "set")).lower()
        if action == "clear":
            with CMD_LOCK:
                MISSION["points"] = []
                MISSION["idx"] = -1
                MISSION["active"] = False
            update_command("idle")
            self._send(200, json.dumps({"ok": True, "mission": mission_payload()}).encode())
            return
        if action == "set":
            # Queue editing only — never starts the aircraft.
            pts = req.get("points")
            if pts is None:
                self._send(400, json.dumps({"ok": False,
                                            "error": "set needs points"}).encode())
                return
            clean, err = self._parse_mission_points(pts)
            if err:
                self._send(400, json.dumps({"ok": False, "error": err,
                                            "area": self._area_json()}).encode())
                return
            with CMD_LOCK:
                MISSION["points"] = clean
                MISSION["idx"] = -1
                MISSION["active"] = False
            update_command("idle")
            self._send(200, json.dumps({"ok": True, "mode": "idle",
                                        "mission": mission_payload()}).encode())
            return
        if action == "start":
            if "points" in req:
                clean, err = self._parse_mission_points(req.get("points"))
                if err:
                    self._send(400, json.dumps({"ok": False, "error": err,
                                                "area": self._area_json()}).encode())
                    return
                with CMD_LOCK:
                    MISSION["points"] = clean
                    MISSION["idx"] = -1
                    MISSION["active"] = False
            with CMD_LOCK:
                if not MISSION["points"]:
                    self._send(400, json.dumps({"ok": False,
                                                "error": "mission has no points"}).encode())
                    return
                MISSION["idx"] = 0
                MISSION["active"] = True
                first = tuple(MISSION["points"][0])
            update_command("go", first)
            self._send(200, json.dumps({"ok": True, "mode": "mission",
                                        "mission": mission_payload()}).encode())
            return
        if action == "skip":
            with CMD_LOCK:
                active = MISSION["active"]
                has_next = MISSION["idx"] + 1 < len(MISSION["points"])
                if active and has_next:
                    MISSION["idx"] += 1
                    nxt = tuple(MISSION["points"][MISSION["idx"]])
                elif active:
                    MISSION["active"] = False
                    MISSION["idx"] = -1
            if not active:
                self._send(409, json.dumps({"ok": False,
                                            "error": "no active mission"}).encode())
                return
            if not has_next:
                update_command("idle")
                self._send(200, json.dumps({"ok": True, "mode": "complete",
                                            "mission": mission_payload()}).encode())
                return
            update_command("go", nxt)
            self._send(200, json.dumps({"ok": True, "mode": "mission",
                                        "mission": mission_payload()}).encode())
            return
        self._send(400, json.dumps({"ok": False,
                                    "error": "unknown mission action (set|start|clear|skip)"}).encode())

    def handle_settings(self, req):
        bounds = {"vmax": (0.2, 10.0), "kp": (0.05, 5.0),
                  "stop_radius": (0.3, 20.0), "hold_deadband": (0.0, 2.0)}
        updates = {}
        for key, (lo, hi) in bounds.items():
            if key in req:
                try:
                    val = float(req[key])
                except (TypeError, ValueError):
                    self._send(400, json.dumps({"ok": False,
                                                "error": f"{key} must be a number"}).encode())
                    return
                if not (lo <= val <= hi):
                    self._send(400, json.dumps({"ok": False,
                                                "error": f"{key} outside [{lo}, {hi}]"}).encode())
                    return
                updates[key] = val
        with CMD_LOCK:
            CTRL.update(updates)
        self._send(200, json.dumps({"ok": True, "ctrl": dict(CTRL)}).encode())


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
    with STATE_LOCK:
        STATE["fix_timeout"] = cfg.position_timeout

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
    elif args.source.startswith(("rtmp", "rtsp")):
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
