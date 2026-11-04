#!/usr/bin/env python3
"""
=============================================================
JAM-ME-DJI — TACTICAL PC CONTROL
=============================================================
Dark tactical console for GPS-free flight:

  LEFT    live downward camera + HUD + fix telemetry
  CENTER  satellite tile map: zoom / pan / click-to-target
  RIGHT   current-position calibration, target entry,
          GO / POSITION HOLD / STOP, waypoint mission queue,
          speed limit and event log

Everything is closed-loop on the VISUAL fix (tile matching).
No GPS is used for navigation. If the fix goes stale the bridge
publishes zero velocities, so the aircraft holds in place.

Run the bridge first (sim sandbox or the real RTMP feed):

  python bridge_server.py --source sim --tiles-dir <tiles>
  python bridge_server.py --source rtmp://127.0.0.1:1935/live

Then:

  python control_gui.py --tiles-dir <same tiles dir>

Map:  click = select target · double-click = GO · drag = pan
      wheel = zoom · F = fit · C = center on drone
Keys: F5 = GO · F6 = HOLD · F7 = STOP · Esc = STOP · Ctrl+Q = quit
=============================================================
"""

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.request
import tkinter as tk

import cv2
import numpy as np

M_LAT = 111320.0

# ---- palette -------------------------------------------------
BG = "#0b0f14"
PANEL = "#111821"
CARD = "#151d28"
CARD_HI = "#1b2531"
BORDER = "#263140"
TEXT = "#dbe4ee"
MUTED = "#7f8ea3"
GREEN = "#3fb950"
BLUE = "#4c8dff"
RED = "#f85149"
AMBER = "#d29922"
DIM = "#5b6b7f"

MONO = ("Consolas", 9)
MONO_S = ("Consolas", 8)
UI = ("Segoe UI", 9)
UI_S = ("Segoe UI", 8)
UI_B = ("Segoe UI Semibold", 9)
UI_BIG = ("Segoe UI Semibold", 13)


# =============================================================
# HTTP CLIENT / BACKGROUND POLLER
# =============================================================
class BridgeClient:
    def __init__(self, host, port):
        self.base = f"http://{host}:{port}"

    def get_json(self, path, timeout=1.0):
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"error": str(e)[:120], "_offline": True}

    def post_json(self, path, payload, timeout=2.0):
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    def snapshot_jpeg(self, timeout=1.5):
        try:
            with urllib.request.urlopen(self.base + "/snapshot", timeout=timeout) as r:
                data = r.read()
            if len(data) < 100:
                return None
            return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            return None


class BridgePoller(threading.Thread):
    """Polls /status + /drive + /snapshot without blocking the UI."""

    def __init__(self, client, interval=0.25):
        super().__init__(daemon=True)
        self.client = client
        self.interval = interval
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.status = {}
        self.drive = {}
        self.snapshot = None
        self.snapshot_seq = 0
        self.online = False
        self.last_error = None

    def run(self):
        last_snap = 0.0
        while not self.stop_evt.is_set():
            st = self.client.get_json("/status")
            dr = self.client.get_json("/drive")
            online = not st.get("_offline", False)
            snap = None
            now = time.monotonic()
            if online and now - last_snap >= 0.45:
                snap = self.client.snapshot_jpeg()
                last_snap = now
            with self.lock:
                self.status = st
                self.drive = dr
                self.online = online
                self.last_error = st.get("error") if online else st.get("error")
                if snap is not None:
                    self.snapshot = snap
                    self.snapshot_seq += 1
            self.stop_evt.wait(self.interval)

    def read(self):
        with self.lock:
            return (dict(self.status), dict(self.drive),
                    self.snapshot, self.snapshot_seq,
                    self.online, self.last_error)


# =============================================================
# SATELLITE MOSAIC (true tile corners, feathered seams)
# =============================================================
class GeoMap:
    def __init__(self, tiles_dir, max_px=2300):
        cfg_path = os.path.join(tiles_dir, "tile_config.json")
        with open(cfg_path) as f:
            self.cfg = json.load(f)
        self.meta = {}
        for t in self.cfg["tiles"]:
            img = cv2.imread(os.path.join(tiles_dir, t["image"]))
            if img is None:
                continue
            self.meta[(t["row"], t["col"])] = (t, img)

        if not self.meta:
            raise RuntimeError("no readable tile images")

        # true bounds of all tile corners
        lats_top, lats_bottom, lons_left, lons_right = [], [], [], []
        for t, _ in self.meta.values():
            c = t["corners"]
            lats_top += [c["top_left"]["lat"], c["top_right"]["lat"]]
            lats_bottom += [c["bottom_left"]["lat"], c["bottom_right"]["lat"]]
            lons_left += [c["top_left"]["lon"], c["bottom_left"]["lon"]]
            lons_right += [c["top_right"]["lon"], c["bottom_right"]["lon"]]
        self.lat_max, self.lat_min = max(lats_top), min(lats_bottom)
        self.lon_min, self.lon_max = min(lons_left), max(lons_right)

        lat_mid = (self.lat_max + self.lat_min) / 2.0
        span_ns = (self.lat_max - self.lat_min) * M_LAT
        span_ew = (self.lon_max - self.lon_min) * M_LAT * math.cos(math.radians(lat_mid))
        if span_ew <= 0 or span_ns <= 0:
            raise RuntimeError("degenerate tile bounds")

        # square-ish pixels: px/m identical on both axes
        if span_ew >= span_ns:
            self.px_w = max_px
            self.px_h = max(64, int(round(max_px * span_ns / span_ew)))
        else:
            self.px_h = max_px
            self.px_w = max(64, int(round(max_px * span_ew / span_ns)))

        acc = np.zeros((self.px_h, self.px_w, 3), np.float32)
        wacc = np.zeros((self.px_h, self.px_w), np.float32)

        for (row, col), (t, img) in self.meta.items():
            c = t["corners"]
            x0 = int(round((c["top_left"]["lon"] - self.lon_min) /
                           (self.lon_max - self.lon_min) * self.px_w))
            x1 = int(round((c["top_right"]["lon"] - self.lon_min) /
                           (self.lon_max - self.lon_min) * self.px_w))
            y0 = int(round((self.lat_max - c["top_left"]["lat"]) /
                           (self.lat_max - self.lat_min) * self.px_h))
            y1 = int(round((self.lat_max - c["bottom_left"]["lat"]) /
                           (self.lat_max - self.lat_min) * self.px_h))
            x0, x1 = max(0, x0), min(self.px_w, x1)
            y0, y1 = max(0, y0), min(self.px_h, y1)
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue

            tile = cv2.resize(img, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)
            h, w = tile.shape[:2]
            # feather edges so overlapping tiles blend instead of showing seams
            f = max(2, int(min(h, w) * 0.07))
            wx = np.ones(w, np.float32)
            wy = np.ones(h, np.float32)
            ramp_x = np.linspace(0.05, 1.0, f, dtype=np.float32)
            ramp_y = np.linspace(0.05, 1.0, f, dtype=np.float32)
            wx[:f] *= ramp_x
            wx[-f:] *= ramp_x[::-1]
            wy[:f] *= ramp_y
            wy[-f:] *= ramp_y[::-1]
            wgt = np.outer(wy, wx)
            acc[y0:y1, x0:x1] += tile.astype(np.float32) * wgt[..., None]
            wacc[y0:y1, x0:x1] += wgt

        wacc = np.maximum(wacc, 1e-4)
        self.mosaic = np.clip(acc / wacc[..., None], 0, 255).astype(np.uint8)
        self.lat_span = self.lat_max - self.lat_min
        self.lon_span = self.lon_max - self.lon_min
        self.mpp = span_ns / self.px_h
        self.n_tiles = len(self.meta)

    # exact linear mapping: tile corners are lat/lon rectangles
    def geo_to_px(self, lat, lon):
        x = (lon - self.lon_min) / self.lon_span * self.px_w
        y = (self.lat_max - lat) / self.lat_span * self.px_h
        return x, y

    def px_to_geo(self, x, y):
        lon = self.lon_min + x / self.px_w * self.lon_span
        lat = self.lat_max - y / self.px_h * self.lat_span
        return lat, lon

    def contains(self, lat, lon):
        return (self.lat_min <= lat <= self.lat_max and
                self.lon_min <= lon <= self.lon_max)


# =============================================================
# INTERACTIVE MAP CANVAS
# =============================================================
class MapCanvas(tk.Canvas):
    def __init__(self, master, gmap, size, on_pick, on_double, on_cursor):
        super().__init__(master, width=size, height=size, bg="#080b10",
                         highlightthickness=0, bd=0)
        self.gmap = gmap
        self.size = size
        self.on_pick = on_pick
        self.on_double = on_double
        self.on_cursor = on_cursor
        self.fit_zoom = min(size / gmap.px_w, size / gmap.px_h)
        self.zoom = self.fit_zoom
        self.cx = gmap.px_w / 2.0
        self.cy = gmap.px_h / 2.0
        self.follow = False
        self._photo = None
        self._photo_key = None
        self._drag = None
        self._moved = False
        self._overlays = True

        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag_move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._double)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Motion>", self._motion)
        self.bind("<Configure>", self._configure)

    # ---- view helpers -----------------------------------------
    def _view(self):
        w = max(2, self.winfo_width() if self.winfo_width() > 1 else self.size)
        h = max(2, self.winfo_height() if self.winfo_height() > 1 else self.size)
        sw = w / self.zoom
        sh = h / self.zoom
        return w, h, self.cx - sw / 2.0, self.cy - sh / 2.0, sw, sh

    def _geo_to_canvas(self, lat, lon, view=None):
        if view is None:
            view = self._view()
        _, _, sx, sy, _, _ = view
        x, y = self.gmap.geo_to_px(lat, lon)
        return (x - sx) * self.zoom, (y - sy) * self.zoom

    def _canvas_to_geo(self, x, y, view=None):
        if view is None:
            view = self._view()
        _, _, sx, sy, _, _ = view
        return self.gmap.px_to_geo(sx + x / self.zoom, sy + y / self.zoom)

    def fit(self):
        self.zoom = self.fit_zoom
        self.cx = self.gmap.px_w / 2.0
        self.cy = self.gmap.px_h / 2.0
        self._photo_key = None
        self.render_base()

    def center_on(self, lat, lon, keep_zoom=True):
        x, y = self.gmap.geo_to_px(lat, lon)
        self.cx, self.cy = x, y
        if not keep_zoom:
            self.zoom = self.fit_zoom
        self._photo_key = None
        self.render_base()

    def set_follow(self, on):
        self.follow = bool(on)

    # ---- base image -------------------------------------------
    def render_base(self, force=False):
        w, h, sx, sy, sw, sh = self._view()
        key = (round(sx, 2), round(sy, 2), round(self.zoom, 4), w, h)
        if not force and key == self._photo_key:
            return
        self._photo_key = key

        x0 = int(math.floor(sx))
        y0 = int(math.floor(sy))
        x1 = int(math.ceil(sx + sw))
        y1 = int(math.ceil(sy + sh))
        pl, pt = max(0, -x0), max(0, -y0)
        pr, pb = max(0, x1 - self.gmap.px_w), max(0, y1 - self.gmap.px_h)
        xs0, xs1 = max(0, x0), min(self.gmap.px_w, x1)
        ys0, ys1 = max(0, y0), min(self.gmap.px_h, y1)
        patch = self.gmap.mosaic[ys0:ys1, xs0:xs1]
        if patch.size == 0:
            patch = np.full((8, 8, 3), 12, np.uint8)
        if pl or pt or pr or pb:
            patch = cv2.copyMakeBorder(patch, pt, pb, pl, pr,
                                       cv2.BORDER_CONSTANT, value=(12, 12, 12))
        if patch.shape[1] != w or patch.shape[0] != h:
            patch = cv2.resize(patch, (w, h), interpolation=cv2.INTER_LINEAR)

        self._photo = self._tk_image(patch)
        self.delete("base")
        self.create_image(0, 0, image=self._photo, anchor="nw", tags="base")
        self.tag_lower("base")

    def _tk_image(self, img):
        ok, ppm = cv2.imencode(".ppm", img)
        if not ok:
            return None
        return tk.PhotoImage(data=ppm.tobytes())

    # ---- overlays ---------------------------------------------
    def redraw_overlays(self, est, target, mission, drive):
        self.delete("ov")
        view = self._view()
        w, h = view[0], view[1]

        def pt(lat, lon):
            return self._geo_to_canvas(lat, lon, view)

        # mission route + waypoints
        if mission:
            pts = mission.get("points") or []
            cur = mission.get("idx", -1)
            if len(pts) > 1:
                coords = []
                for p in pts:
                    coords.extend(pt(p[0], p[1]))
                self.create_line(*coords, fill="#2b5f9e", width=2,
                                 dash=(5, 4), tags="ov")
            for i, p in enumerate(pts):
                x, y = pt(p[0], p[1])
                if not (-20 <= x <= w + 20 and -20 <= y <= h + 20):
                    continue
                color = AMBER if i == cur else "#4c8dff"
                self.create_oval(x - 7, y - 7, x + 7, y + 7, outline=color,
                                 width=2, tags="ov")
                self.create_text(x, y, text=str(i + 1), fill=color,
                                 font=("Segoe UI Semibold", 7), tags="ov")

        # target crosshair + line of sight + distance label
        if target:
            tx, ty = pt(target[0], target[1])
            if -40 <= tx <= w + 40 and -40 <= ty <= h + 40:
                self.create_line(tx - 13, ty, tx + 13, ty, fill="#ff5c5c",
                                 width=2, tags="ov")
                self.create_line(tx, ty - 13, tx, ty + 13, fill="#ff5c5c",
                                 width=2, tags="ov")
                self.create_oval(tx - 7, ty - 7, tx + 7, ty + 7,
                                 outline="#ff5c5c", width=1, tags="ov")
                self.create_text(tx + 15, ty - 13, text="TARGET", anchor="w",
                                 fill="#ff8a8a", font=("Segoe UI Semibold", 8),
                                 tags="ov")
                if est:
                    dx, dy = pt(est["lat"], est["lon"])
                    self.create_line(dx, dy, tx, ty, fill="#8a4444",
                                     width=1, dash=(3, 3), tags="ov")
                    dm, brg = distance_bearing(est["lat"], est["lon"],
                                               target[0], target[1])
                    mx, my = (dx + tx) / 2, (dy + ty) / 2
                    self.create_text(mx + 5, my - 7, text=f"{dm:.0f} m / {brg:.0f}°",
                                     anchor="w", fill="#d9b38c",
                                     font=("Segoe UI", 8), tags="ov")

        # drone + heading
        if est:
            x, y = pt(est["lat"], est["lon"])
            heading = est.get("heading", 0.0) or 0.0
            rad = math.radians(heading)
            dx, dy = math.sin(rad), -math.cos(rad)
            self.create_oval(x - 10, y - 10, x + 10, y + 10,
                             outline="#2f9e44", width=2, tags="ov")
            self.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#3fb950",
                             outline="", tags="ov")
            self.create_line(x, y, x + dx * 26, y + dy * 26,
                             fill="#3fb950", width=3, arrow="last", tags="ov")
            conf = float(est.get("conf") or 0.0)
            r = 12 + (1.0 - min(1.0, conf)) * 26
            self.create_oval(x - r, y - r, x + r, y + r,
                             outline="#2f6b3d", width=1, tags="ov")

        # scale bar
        disp_mpp = self.gmap.mpp / self.zoom
        candidates = [5, 10, 20, 50, 100, 200, 500, 1000]
        target_px = 90
        meters = min(candidates, key=lambda m: abs(m / disp_mpp - target_px))
        px = meters / disp_mpp
        x0, y0 = 16, h - 20
        self.create_line(x0, y0, x0 + px, y0, fill="#c8d4e0", width=2, tags="ov")
        self.create_line(x0, y0 - 4, x0, y0 + 4, fill="#c8d4e0", width=2, tags="ov")
        self.create_line(x0 + px, y0 - 4, x0 + px, y0 + 4, fill="#c8d4e0", width=2, tags="ov")
        self.create_text(x0 + px / 2, y0 - 10, text=f"{meters} m", fill="#c8d4e0",
                         font=("Segoe UI", 8), tags="ov")

        # north arrow
        nx, ny = w - 28, 30
        self.create_line(nx, ny + 14, nx, ny - 12, fill="#c8d4e0", width=2,
                         arrow="last", tags="ov")
        self.create_text(nx, ny - 22, text="N", fill="#c8d4e0",
                         font=("Segoe UI Semibold", 9), tags="ov")

        # frame corners / reticle
        for cx, cy, ax, ay in ((0, 0, 1, 1), (w, 0, -1, 1),
                               (0, h, 1, -1), (w, h, -1, -1)):
            self.create_line(cx, cy, cx + ax * 16, cy, fill="#2c3a4c", width=2, tags="ov")
            self.create_line(cx, cy, cx, cy + ay * 16, fill="#2c3a4c", width=2, tags="ov")

    # ---- interaction ------------------------------------------
    def _press(self, e):
        self._drag = (e.x, e.y)
        self._moved = False

    def _drag_move(self, e):
        if self._drag is None:
            return
        dx = e.x - self._drag[0]
        dy = e.y - self._drag[1]
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.cx -= dx / self.zoom
        self.cy -= dy / self.zoom
        self._drag = (e.x, e.y)
        self._photo_key = None
        self.render_base()

    def _release(self, e):
        if not self._moved and self._drag is not None:
            lat, lon = self._canvas_to_geo(e.x, e.y)
            if self.gmap.contains(lat, lon):
                self.on_pick(lat, lon)
        self._drag = None

    def _double(self, e):
        lat, lon = self._canvas_to_geo(e.x, e.y)
        if self.gmap.contains(lat, lon):
            self.on_double(lat, lon)

    def _wheel(self, e):
        if e.delta == 0:
            return
        view = self._view()
        _, _, _, _, _, _ = view
        mx, my = self._canvas_to_geo(e.x, e.y, view)
        factor = 1.22 ** (e.delta / 120.0)
        new_zoom = min(max(self.zoom * factor, self.fit_zoom * 0.9),
                       max(2.0, self.gmap.mpp / 0.12))
        if abs(new_zoom - self.zoom) < 1e-6:
            return
        self.zoom = new_zoom
        # keep the geo point under the cursor fixed
        x, y = self.gmap.geo_to_px(mx, my)
        w, h = view[0], view[1]
        self.cx = x - (e.x - w / 2.0) / self.zoom
        self.cy = y - (e.y - h / 2.0) / self.zoom
        self._photo_key = None
        self.render_base()

    def _motion(self, e):
        lat, lon = self._canvas_to_geo(e.x, e.y)
        self.on_cursor(lat, lon)

    def _configure(self, _e):
        self._photo_key = None
        self.render_base()


# =============================================================
# SMALL UI HELPERS
# =============================================================
def mk_card(parent, title, pady=(0, 6)):
    outer = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                     highlightthickness=1)
    outer.pack(fill="x", pady=pady)
    head = tk.Frame(outer, bg=CARD)
    head.pack(fill="x", padx=10, pady=(6, 2))
    tk.Label(head, text=title, bg=CARD, fg=MUTED, font=UI_S,
             anchor="w").pack(side="left")
    body = tk.Frame(outer, bg=CARD)
    body.pack(fill="both", expand=True, padx=10, pady=(0, 8))
    return outer, body, head


def mk_entry(parent, textvariable, width=14):
    e = tk.Entry(parent, textvariable=textvariable, width=width,
                 bg="#0c131b", fg=TEXT, insertbackground=TEXT,
                 relief="flat", highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=BLUE,
                 font=MONO, justify="left")
    return e


def mk_button(parent, text, bg, command, fg="white", font=UI_B,
              height=1, width=None, active=None, state="normal"):
    kw = dict(text=text, command=command, bg=bg, fg=fg, font=font,
              relief="flat", bd=0, highlightthickness=0,
              activebackground=active or bg, activeforeground=fg,
              cursor="hand2", height=height, state=state)
    if width is not None:
        kw["width"] = width
    b = tk.Button(parent, **kw)
    return b


def distance_bearing(lat1, lon1, lat2, lon2):
    dn = (lat2 - lat1) * M_LAT
    de = (lon2 - lon1) * M_LAT * math.cos(math.radians((lat1 + lat2) / 2.0))
    dist = math.hypot(dn, de)
    brg = math.degrees(math.atan2(de, dn)) % 360.0
    return dist, brg


def fmt_lat(v):
    return f"{v:.7f}"


def fmt_lon(v):
    return f"{v:.7f}"


# =============================================================
# MAIN APPLICATION
# =============================================================
class TacticalApp:
    def __init__(self, client: BridgeClient, tiles_dir: str):
        self.client = client
        self.root = tk.Tk()
        self.root.title("JAM-ME  ·  Tactical Pilot  ·  GPS-free visual navigation")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.gmap = GeoMap(tiles_dir)
        self.status = {}
        self.drive = {}
        self.mission = {}
        self.target = None
        self._last_snap_seq = -1
        self._last_online = None
        self._speed_init = False
        self._suspend_target = False
        self._suspend_pos = False
        self._tick_count = 0

        self.poller = BridgePoller(client)
        self.poller.start()

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.video_w = min(620, max(420, int(sw * 0.32)))
        self.video_h = int(self.video_w * 3 / 4)
        self.map_size = min(760, max(480, int(sw * 0.38)), sh - 150)
        # keep the whole console inside the screen
        if self.video_w + self.map_size + 390 > sw - 20:
            over = self.video_w + self.map_size + 390 - (sw - 20)
            self.map_size = max(420, self.map_size - over)

        self.pos_lat = tk.StringVar()
        self.pos_lon = tk.StringVar()
        self.tgt_lat = tk.StringVar()
        self.tgt_lon = tk.StringVar()
        self.speed_var = tk.DoubleVar(value=2.5)

        self._build_topbar()
        self._build_body()
        self._build_statusbar()

        self.msg(f"map loaded: {self.gmap.n_tiles} tiles · "
                 f"{self.gmap.mpp:.2f} m/px mosaic")
        self.bind_keys()

        self._last_follow = False
        self.root.after(120, self.refresh)

    # ---- UI construction --------------------------------------
    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=PANEL, height=54)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=PANEL)
        left.pack(side="left", padx=14, pady=6)
        tk.Label(left, text="JAM-ME", bg=PANEL, fg=TEXT,
                 font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(left, text="GPS-FREE VISUAL NAVIGATION  ·  DJI MINI 4 PRO",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 7)).pack(anchor="w")

        mid = tk.Frame(bar, bg=PANEL)
        mid.pack(side="left", padx=26, pady=8)
        self.link_dot = tk.Label(mid, text="●", bg=PANEL, fg=RED,
                                 font=("Segoe UI", 12))
        self.link_dot.pack(side="left")
        self.link_label = tk.Label(mid, text="BRIDGE OFFLINE", bg=PANEL,
                                   fg=MUTED, font=UI_B)
        self.link_label.pack(side="left", padx=(4, 16))

        self.fix_pill = tk.Label(mid, text="FIX —", bg=BG, fg=MUTED,
                                 font=UI_B, padx=10, pady=3)
        self.fix_pill.pack(side="left", padx=4)

        right = tk.Frame(bar, bg=PANEL)
        right.pack(side="right", padx=14, pady=8)
        self.clock = tk.Label(right, text="", bg=PANEL, fg=MUTED, font=MONO)
        self.clock.pack(side="right", padx=(12, 0))
        self.mode_pill = tk.Label(right, text="IDLE", bg=BG, fg=MUTED,
                                  font=UI_B, padx=12, pady=3)
        self.mode_pill.pack(side="right")

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        # ---------------- LEFT: camera + telemetry -------------
        left = tk.Frame(body, bg=BG, width=self.video_w)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)

        hdr = tk.Frame(left, bg=BG)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="LIVE DOWNLINK", bg=BG, fg=TEXT,
                 font=UI_B).pack(side="left")
        self.cam_state = tk.Label(hdr, text="WAITING", bg=BG, fg=AMBER,
                                  font=UI_S)
        self.cam_state.pack(side="right")

        self.video_holder = tk.Frame(left, bg="#000000", width=self.video_w,
                                     height=self.video_h,
                                     highlightbackground=BORDER,
                                     highlightthickness=1)
        self.video_holder.pack_propagate(False)
        self.video_holder.pack(fill="x")
        self.video_label = tk.Label(self.video_holder, bg="#000000", bd=0)
        self.video_label.pack(fill="both", expand=True)

        # telemetry grid
        tele = tk.Frame(left, bg=BG)
        tele.pack(fill="x", pady=(8, 0))
        self.stat = {}
        cells = [("FIX", "—"), ("CONF", "—"), ("MATCH", "—"),
                 ("TILE", "—"), ("AGE", "—"), ("HEAD", "—")]
        for i, (name, val) in enumerate(cells):
            r, c = divmod(i, 3)
            box = tk.Frame(tele, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1)
            box.grid(row=r, column=c, sticky="nsew", padx=(0, 4), pady=(0, 4))
            tk.Label(box, text=name, bg=CARD, fg=MUTED, font=UI_S,
                     anchor="w").pack(fill="x", padx=7, pady=(3, 0))
            lab = tk.Label(box, text=val, bg=CARD, fg=TEXT, font=MONO,
                           anchor="w")
            lab.pack(fill="x", padx=7, pady=(0, 3))
            self.stat[name] = lab
        for c in range(3):
            tele.grid_columnconfigure(c, weight=1)

        # velocity strip
        vel = tk.Frame(left, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1)
        vel.pack(fill="x", pady=(4, 0))
        tk.Label(vel, text="CONTROL", bg=CARD, fg=MUTED,
                 font=UI_S).pack(side="left", padx=(8, 10), pady=5)
        self.vel_label = tk.Label(vel, text="v_f +0.00   v_r +0.00   dist —",
                                  bg=CARD, fg=TEXT, font=MONO)
        self.vel_label.pack(side="left")

        # ---------------- CENTER: map ---------------------------
        center = tk.Frame(body, bg=BG)
        center.pack(side="left", fill="both", expand=True, padx=(0, 8))

        hdr2 = tk.Frame(center, bg=BG)
        hdr2.pack(fill="x", pady=(0, 4))
        tk.Label(hdr2, text="TACTICAL MAP", bg=BG, fg=TEXT,
                 font=UI_B).pack(side="left")
        self.map_hint = tk.Label(hdr2, text="click = target · drag = pan · wheel = zoom",
                                 bg=BG, fg=MUTED, font=UI_S)
        self.map_hint.pack(side="right")

        self.map = MapCanvas(center, self.gmap, self.map_size,
                             on_pick=self.on_map_pick,
                             on_double=self.on_map_double,
                             on_cursor=self.on_cursor_geo)
        self.map.pack()

        tools = tk.Frame(center, bg=BG)
        tools.pack(fill="x", pady=(4, 0))
        self.cursor_label = tk.Label(tools, text="lat —  lon —", bg=BG,
                                     fg=MUTED, font=MONO_S)
        self.cursor_label.pack(side="left")
        mk_button(tools, "FIT", CARD_HI, self.map.fit, fg=TEXT,
                  font=UI_S).pack(side="right", padx=(4, 0))
        self.follow_btn = mk_button(tools, "FOLLOW: OFF", CARD_HI,
                                    self.toggle_follow, fg=TEXT, font=UI_S)
        self.follow_btn.pack(side="right", padx=(4, 0))
        mk_button(tools, "+", CARD_HI, lambda: self.zoom_step(1.25),
                  fg=TEXT, font=UI_S, width=2).pack(side="right", padx=(4, 0))
        mk_button(tools, "−", CARD_HI, lambda: self.zoom_step(0.8),
                  fg=TEXT, font=UI_S, width=2).pack(side="right", padx=(4, 0))
        mk_button(tools, "DRONE", CARD_HI, self.center_drone, fg=TEXT,
                  font=UI_S).pack(side="right", padx=(4, 0))

        # ---------------- RIGHT: controls -----------------------
        right = tk.Frame(body, bg=BG, width=378)
        right.pack(side="right", fill="both")
        right.pack_propagate(False)

        # current position / calibration
        _, body1, _ = mk_card(right, "CURRENT POSITION  ·  CALIBRATION")
        row = tk.Frame(body1, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="LAT", bg=CARD, fg=MUTED, font=UI_S,
                 width=4, anchor="w").pack(side="left")
        mk_entry(row, self.pos_lat, width=15).pack(side="left", padx=(0, 6))
        tk.Label(row, text="LON", bg=CARD, fg=MUTED, font=UI_S,
                 width=4, anchor="w").pack(side="left")
        mk_entry(row, self.pos_lon, width=15).pack(side="left")

        brow = tk.Frame(body1, bg=CARD)
        brow.pack(fill="x", pady=(5, 0))
        mk_button(brow, "USE FIX", CARD_HI, self.use_fix, fg=TEXT,
                  font=UI_S).pack(side="left", padx=(0, 4))
        mk_button(brow, "SET POSITION", BLUE, self.set_position,
                  font=UI_S).pack(side="left", padx=(0, 4))
        mk_button(brow, "CLEAR", CARD_HI, self.clear_calibration, fg=MUTED,
                  font=UI_S).pack(side="left")
        self.cal_label = tk.Label(body1, text="calibration: none",
                                  bg=CARD, fg=MUTED, font=UI_S, anchor="w")
        self.cal_label.pack(fill="x", pady=(4, 0))

        # target / flight control
        _, body2, _ = mk_card(right, "TARGET")
        row = tk.Frame(body2, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="LAT", bg=CARD, fg=MUTED, font=UI_S,
                 width=4, anchor="w").pack(side="left")
        mk_entry(row, self.tgt_lat, width=15).pack(side="left", padx=(0, 6))
        tk.Label(row, text="LON", bg=CARD, fg=MUTED, font=UI_S,
                 width=4, anchor="w").pack(side="left")
        mk_entry(row, self.tgt_lon, width=15).pack(side="left")

        self.tgt_trace = (self.tgt_lat.trace_add("write", self.on_target_edit),
                          self.tgt_lon.trace_add("write", self.on_target_edit))

        self.target_info = tk.Label(body2, text="distance —   bearing —",
                                    bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 11))
        self.target_info.pack(fill="x", pady=(6, 4))

        self.btn_go = mk_button(body2, "▶   GO TO TARGET", "#1f8f3a",
                                self.issue_go, font=("Segoe UI Semibold", 11),
                                height=2)
        self.btn_go.pack(fill="x", pady=(2, 4))
        row = tk.Frame(body2, bg=CARD)
        row.pack(fill="x")
        self.btn_hold = mk_button(row, "POSITION HOLD", "#1f5fa8",
                                  self.issue_hold, font=UI_B)
        self.btn_hold.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.btn_stop = mk_button(row, "STOP", "#a52a2a", self.issue_stop,
                                  font=UI_B)
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=(3, 0))

        # mission
        _, body3, _ = mk_card(right, "MISSION QUEUE")
        self.mission_list = tk.Listbox(body3, height=4, bg="#0c131b",
                                       fg=TEXT, selectbackground="#25405e",
                                       selectforeground="white", font=MONO_S,
                                       relief="flat", highlightthickness=1,
                                       highlightbackground=BORDER,
                                       highlightcolor=BLUE,
                                       activestyle="none")
        self.mission_list.pack(fill="x")
        self.mission_info = tk.Label(body3, text="idle · 0 points",
                                     bg=CARD, fg=MUTED, font=UI_S, anchor="w")
        self.mission_info.pack(fill="x", pady=(3, 3))
        row = tk.Frame(body3, bg=CARD)
        row.pack(fill="x")
        mk_button(row, "ADD TARGET", CARD_HI, self.mission_add, fg=TEXT,
                  font=UI_S).pack(side="left", padx=(0, 4))
        mk_button(row, "REMOVE", CARD_HI, self.mission_remove, fg=TEXT,
                  font=UI_S).pack(side="left", padx=(0, 4))
        mk_button(row, "CLEAR", CARD_HI, self.mission_clear, fg=MUTED,
                  font=UI_S).pack(side="left")
        row2 = tk.Frame(body3, bg=CARD)
        row2.pack(fill="x", pady=(4, 0))
        mk_button(row2, "START MISSION", "#1f8f3a", self.mission_start,
                  font=UI_S).pack(side="left", padx=(0, 4))
        mk_button(row2, "SKIP", "#1f5fa8", self.mission_skip,
                  font=UI_S).pack(side="left")

        # speed
        _, body4, _ = mk_card(right, "SPEED LIMIT")
        srow = tk.Frame(body4, bg=CARD)
        srow.pack(fill="x")
        self.speed_label = tk.Label(srow, text="2.5 m/s", bg=CARD, fg=TEXT,
                                    font=MONO, width=8, anchor="w")
        self.speed_label.pack(side="right")
        self.speed_scale = tk.Scale(srow, from_=0.5, to=5.0, resolution=0.1,
                                    orient="horizontal", variable=self.speed_var,
                                    showvalue=0, bg=CARD, fg=TEXT,
                                    troughcolor="#0c131b", highlightthickness=0,
                                    bd=0, sliderrelief="flat",
                                    activebackground=BLUE,
                                    command=self.on_speed_move)
        self.speed_scale.pack(side="left", fill="x", expand=True)
        self.speed_scale.bind("<ButtonRelease-1>", self.on_speed_release)

        # log
        logcard = tk.Frame(right, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1)
        logcard.pack(fill="both", expand=True, pady=(0, 0))
        tk.Label(logcard, text="EVENT LOG", bg=CARD, fg=MUTED, font=UI_S,
                 anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        self.log_text = tk.Text(logcard, height=4, bg="#0b1118", fg="#9fb0c4",
                                insertbackground=TEXT, font=MONO_S,
                                relief="flat", highlightthickness=0,
                                state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 8))

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=PANEL, height=24)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.msg_label = tk.Label(bar, text="ready", bg=PANEL, fg=MUTED,
                                  font=UI_S, anchor="w")
        self.msg_label.pack(side="left", padx=10)
        self.bridge_label = tk.Label(bar, text=self.client.base, bg=PANEL,
                                     fg=DIM, font=MONO_S, anchor="e")
        self.bridge_label.pack(side="right", padx=10)

    def bind_keys(self):
        self.root.bind("<F5>", lambda e: self.issue_go())
        self.root.bind("<F6>", lambda e: self.issue_hold())
        self.root.bind("<F7>", lambda e: self.issue_stop())
        self.root.bind("<Escape>", lambda e: self.issue_stop())
        self.root.bind("<Control-q>", lambda e: self.quit())
        self.root.bind("f", lambda e: self._key_map(lambda: self.map.fit()))
        self.root.bind("c", lambda e: self._key_map(self.center_drone))

    def _key_map(self, fn):
        # avoid hijacking letters while typing in an entry
        try:
            if isinstance(self.root.focus_get(), tk.Entry):
                return
        except Exception:
            pass
        fn()

    # ---- helpers ----------------------------------------------
    def msg(self, text):
        self.msg_label.configure(text=text)

    def log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 120:
            self.log_text.delete("1.0", f"{line_count - 120}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.msg(text)

    def current_fix(self):
        est = self.status.get("estimated")
        return est if isinstance(est, dict) else None

    def parse_coords(self, lat_s, lon_s):
        try:
            lat = float(lat_s)
            lon = float(lon_s)
        except (TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon

    def target_from_entries(self):
        return self.parse_coords(self.tgt_lat.get().strip(),
                                 self.tgt_lon.get().strip())

    # ---- map callbacks ----------------------------------------
    def on_map_pick(self, lat, lon):
        self.set_target(lat, lon, log_it=True)

    def on_map_double(self, lat, lon):
        self.set_target(lat, lon, log_it=True)
        self.issue_go()

    def set_target(self, lat, lon, log_it=True):
        self._suspend_target = True
        self.tgt_lat.set(f"{lat:.7f}")
        self.tgt_lon.set(f"{lon:.7f}")
        self._suspend_target = False
        self.target = (lat, lon)
        self.update_target_info()
        if log_it:
            self.log(f"target selected  {lat:.7f}, {lon:.7f}")

    def on_target_edit(self, *_):
        if self._suspend_target:
            return
        t = self.target_from_entries()
        self.target = t
        self.update_target_info()

    def update_target_info(self):
        est = self.current_fix()
        if est and self.target:
            d, b = distance_bearing(est["lat"], est["lon"],
                                    self.target[0], self.target[1])
            self.target_info.configure(text=f"distance {d:8.1f} m     bearing {b:05.1f}°")
        else:
            self.target_info.configure(text="distance —    bearing —")

    def on_cursor_geo(self, lat, lon):
        self.cursor_label.configure(text=f"lat {lat:.7f}   lon {lon:.7f}")

    def toggle_follow(self):
        self.map.set_follow(not self.map.follow)
        self.follow_btn.configure(
            text=f"FOLLOW: {'ON' if self.map.follow else 'OFF'}",
            fg=GREEN if self.map.follow else TEXT)

    def center_drone(self):
        est = self.current_fix()
        if est:
            self.map.center_on(est["lat"], est["lon"])
        else:
            self.msg("no visual fix to center on")

    def zoom_step(self, factor):
        # reuse wheel math around the canvas center
        class _E:
            pass
        e = _E()
        e.x = self.map.winfo_width() // 2
        e.y = self.map.winfo_height() // 2
        e.delta = 120 if factor > 1 else -120
        self.map._wheel(e)

    # ---- calibration ------------------------------------------
    def use_fix(self):
        est = self.current_fix()
        if not est:
            self.msg("no visual fix yet")
            return
        self.pos_lat.set(f"{est['lat']:.7f}")
        self.pos_lon.set(f"{est['lon']:.7f}")

    def set_position(self):
        c = self.parse_coords(self.pos_lat.get().strip(),
                              self.pos_lon.get().strip())
        if not c:
            self.log("position: enter a valid lat/lon")
            return
        r = self.client.post_json("/calibrate", {"lat": c[0], "lon": c[1]})
        if r.get("ok"):
            self.log(f"position anchored at {c[0]:.7f}, {c[1]:.7f}  "
                     f"(offset {r.get('offset_m', '?')} m)")
        else:
            self.log(f"calibration failed: {r.get('error', '?')}")

    def clear_calibration(self):
        r = self.client.post_json("/calibrate", {"action": "clear"})
        if r.get("ok"):
            self.log("calibration cleared")
        else:
            self.log(f"calibration clear failed: {r.get('error', '?')}")

    # ---- commands ---------------------------------------------
    def issue_go(self):
        t = self.target_from_entries()
        if not t:
            self.log("GO: select a target on the map or type lat/lon")
            return
        r = self.client.post_json("/command", {"action": "go",
                                               "lat": t[0], "lon": t[1]})
        if r.get("ok"):
            self.set_target(t[0], t[1], log_it=False)
            self.log(f"GO  {t[0]:.7f}, {t[1]:.7f}")
        else:
            self.log(f"GO rejected: {r.get('error', '?')}")

    def issue_hold(self):
        r = self.client.post_json("/command", {"action": "hold"})
        if r.get("ok"):
            self.log("POSITION HOLD at current visual fix")
        else:
            self.log(f"HOLD rejected: {r.get('error', '?')}")

    def issue_stop(self):
        r = self.client.post_json("/command", {"action": "stop"})
        self.log("STOP — zero velocities" if r.get("ok")
                 else f"STOP failed: {r.get('error', '?')}")

    # ---- mission ----------------------------------------------
    def mission_points(self):
        m = self.status.get("mission") or {}
        return [list(p) for p in (m.get("points") or [])]

    def mission_active(self):
        return bool((self.status.get("mission") or {}).get("active"))

    def push_mission(self, points, action="set"):
        r = self.client.post_json("/mission",
                                  {"action": action, "points": points})
        if not r.get("ok"):
            self.log(f"mission failed: {r.get('error', '?')}")
            return False
        if r.get("mission"):
            self.status["mission"] = r["mission"]
            self.update_mission(r["mission"])
        self._last_mission_key = None
        return True

    def mission_add(self):
        if self.mission_active():
            self.log("mission is running — STOP first")
            return
        t = self.target_from_entries()
        if not t:
            self.log("ADD: select a target first")
            return
        pts = self.mission_points() + [[t[0], t[1]]]
        if self.push_mission(pts, "set"):
            self.log(f"mission: added point {len(pts)}")

    def mission_remove(self):
        if self.mission_active():
            self.log("mission is running — STOP first")
            return
        sel = self.mission_list.curselection()
        if not sel:
            self.log("mission: select a row to remove")
            return
        pts = self.mission_points()
        idx = int(sel[0])
        if 0 <= idx < len(pts):
            pts.pop(idx)
            if self.push_mission(pts, "set"):
                self.log(f"mission: removed point {idx + 1}")

    def mission_clear(self):
        r = self.client.post_json("/mission", {"action": "clear"})
        if r.get("ok"):
            if r.get("mission"):
                self.status["mission"] = r["mission"]
                self.update_mission(r["mission"])
            self._last_mission_key = None
            self.log("mission cleared")
        else:
            self.log(f"mission clear failed: {r.get('error', '?')}")

    def mission_start(self):
        pts = self.mission_points()
        if not pts:
            self.log("mission: queue is empty")
            return
        r = self.client.post_json("/mission", {"action": "start", "points": pts})
        if r.get("ok"):
            if r.get("mission"):
                self.status["mission"] = r["mission"]
                self.update_mission(r["mission"])
            self._last_mission_key = None
            self.log(f"mission started · {len(pts)} waypoints")
        else:
            self.log(f"mission start failed: {r.get('error', '?')}")

    def mission_skip(self):
        r = self.client.post_json("/mission", {"action": "skip"})
        if r.get("ok"):
            if r.get("mission"):
                self.status["mission"] = r["mission"]
                self.update_mission(r["mission"])
            self._last_mission_key = None
            self.log("mission: skipped to next waypoint")
        else:
            self.log(f"mission skip failed: {r.get('error', '?')}")

    # ---- speed ------------------------------------------------
    def on_speed_move(self, value):
        self.speed_label.configure(text=f"{float(value):.1f} m/s")

    def on_speed_release(self, _e=None):
        v = float(self.speed_var.get())
        r = self.client.post_json("/settings", {"vmax": v})
        if r.get("ok"):
            self.log(f"speed limit set to {v:.1f} m/s")
        else:
            self.log(f"speed change failed: {r.get('error', '?')}")

    # ---- camera HUD -------------------------------------------
    def draw_hud(self, img):
        if img is None:
            return None
        out = img.copy()
        h, w = out.shape[:2]
        est = self.current_fix()
        valid = bool(self.status.get("fix_valid"))
        drive = self.drive or {}
        mode = str(drive.get("mode", "idle")).upper()
        blind = bool(drive.get("blind"))

        border = (70, 190, 90) if valid else (60, 170, 240)
        if blind:
            border = (60, 60, 240)
        cv2.rectangle(out, (0, 0), (w - 1, h - 1), border, 3)

        # top strip
        strip = out.copy()
        cv2.rectangle(strip, (0, 0), (w, 34), (10, 14, 20), -1)
        cv2.addWeighted(strip, 0.62, out, 0.38, 0, out)
        cv2.putText(out, "LIVE  ·  DOWNWARD", (12, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (235, 235, 235), 1, cv2.LINE_AA)
        quality = f"FIX {est['conf'] * 100:.0f}%" if est else "NO FIX"
        col = (90, 210, 100) if valid else (60, 170, 240)
        cv2.putText(out, quality, (w - 118, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, col, 1, cv2.LINE_AA)

        # bottom strip
        strip = out.copy()
        cv2.rectangle(strip, (0, h - 42), (w, h), (10, 14, 20), -1)
        cv2.addWeighted(strip, 0.66, out, 0.34, 0, out)
        if est:
            cv2.putText(out, f"{est['lat']:.7f}  {est['lon']:.7f}", (12, h - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (215, 215, 215), 1, cv2.LINE_AA)
        cv2.putText(out, mode, (w - 96, h - 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (90, 210, 100) if mode == "GO" else (220, 220, 220), 1, cv2.LINE_AA)

        # center reticle
        cx, cy = w // 2, h // 2
        cv2.line(out, (cx - 26, cy), (cx - 8, cy), (200, 200, 200), 1)
        cv2.line(out, (cx + 8, cy), (cx + 26, cy), (200, 200, 200), 1)
        cv2.line(out, (cx, cy - 26), (cx, cy - 8), (200, 200, 200), 1)
        cv2.line(out, (cx, cy + 8), (cx, cy + 26), (200, 200, 200), 1)
        cv2.circle(out, (cx, cy), 2, (80, 220, 120), -1)

        # target bearing arrow around the reticle
        tgt = self.target
        if est and tgt:
            d, b = distance_bearing(est["lat"], est["lon"], tgt[0], tgt[1])
            head = float(est.get("heading") or 0.0)
            rel = math.radians(b - head)
            ax = int(cx + math.sin(rel) * 52)
            ay = int(cy - math.cos(rel) * 52)
            cv2.arrowedLine(out, (cx, cy), (ax, ay), (80, 180, 255), 2,
                            tipLength=0.18)
            cv2.putText(out, f"{d:.0f} m", (ax + 6, ay - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (120, 200, 255), 1,
                        cv2.LINE_AA)
        if blind:
            cv2.putText(out, "BLIND — HOLDING", (12, h - 52),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 2, cv2.LINE_AA)

        # fit into the left panel (letterbox, never stretch)
        vw, vh = self.video_w, self.video_h
        scale = min(vw / w, vh / h)
        rw, rh = max(1, int(w * scale)), max(1, int(h * scale))
        resized = cv2.resize(out, (rw, rh), interpolation=cv2.INTER_AREA)
        frame = np.full((vh, vw, 3), 8, np.uint8)
        ox, oy = (vw - rw) // 2, (vh - rh) // 2
        frame[oy:oy + rh, ox:ox + rw] = resized
        return frame

    def update_camera(self, img, seq):
        if seq == self._last_snap_seq:
            return
        self._last_snap_seq = seq
        if img is None:
            self.video_label.configure(image="", text="waiting for video…",
                                       fg=MUTED)
            self.cam_state.configure(text="WAITING", fg=AMBER)
            return
        frame = self.draw_hud(img)
        ok, ppm = cv2.imencode(".ppm", frame)
        if not ok:
            return
        self._video_photo = tk.PhotoImage(data=ppm.tobytes())
        self.video_label.configure(image=self._video_photo, text="")
        self.cam_state.configure(text="STREAMING", fg=GREEN)

    # ---- periodic refresh -------------------------------------
    def refresh(self):
        status, drive, snap, seq, online, err = self.poller.read()
        self.status = status
        self.drive = drive
        self._tick_count += 1

        # top bar state
        if online:
            self.link_dot.configure(fg=GREEN)
            self.link_label.configure(text=f"BRIDGE LINKED · {status.get('tiles_loaded', '?')} TILES",
                                      fg=TEXT)
        else:
            self.link_dot.configure(fg=RED)
            self.link_label.configure(text="BRIDGE OFFLINE", fg=MUTED)

        est = self.current_fix()
        valid = bool(status.get("fix_valid"))
        if valid and est:
            self.fix_pill.configure(text=f"FIX VALID · {est.get('conf', 0) * 100:.0f}%",
                                    bg="#12351d", fg=GREEN)
        else:
            self.fix_pill.configure(text="FIX STALE" if est else "NO FIX",
                                    bg="#33240c", fg=AMBER if est else MUTED)

        mode = str(drive.get("mode", "idle")).upper()
        mode_colors = {"GO": ("#12351d", GREEN), "HOLD": ("#102b4c", BLUE),
                       "COMPLETE": ("#12351d", GREEN), "IDLE": (BG, MUTED),
                       "MISSION": ("#33240c", AMBER)}
        mbg, mfg = mode_colors.get(mode, (BG, MUTED))
        self.mode_pill.configure(text=mode, bg=mbg, fg=mfg)
        self.clock.configure(text=time.strftime("%H:%M:%S"))

        # telemetry
        if est:
            self.stat["FIX"].configure(text="VALID" if valid else "STALE",
                                       fg=GREEN if valid else AMBER)
            self.stat["CONF"].configure(text=f"{float(est.get('conf', 0)) * 100:.0f}%")
            self.stat["MATCH"].configure(text=str(est.get("matches", "—")))
            self.stat["TILE"].configure(text=str(est.get("tile", "—")))
            age = status.get("age_s")
            self.stat["AGE"].configure(text=f"{age:.1f}s" if age is not None else "—")
            self.stat["HEAD"].configure(text=f"{float(est.get('heading') or 0):03.0f}°")
            if self._tick_count % 4 == 0:
                cal = status.get("cal_offset", {})
                if cal.get("enabled"):
                    dlat_m = cal.get("dlat", 0) * M_LAT
                    dlon_m = cal.get("dlon", 0) * M_LAT * math.cos(
                        math.radians(est.get("lat", 0)))
                    self.cal_label.configure(
                        text=f"calibration: ON · {dlat_m:+.1f} m N / {dlon_m:+.1f} m E",
                        fg=GREEN)
                else:
                    self.cal_label.configure(text="calibration: none", fg=MUTED)
        else:
            for k in ("FIX", "CONF", "MATCH", "TILE", "AGE", "HEAD"):
                self.stat[k].configure(text="—", fg=TEXT)
            self.stat["FIX"].configure(text="NO FIX", fg=AMBER)

        dist = drive.get("dist_m")
        self.vel_label.configure(
            text=f"v_f {drive.get('v_forward', 0):+.2f}   "
                 f"v_r {drive.get('v_right', 0):+.2f}   "
                 f"dist {dist:.1f} m" if dist is not None else
                 f"v_f {drive.get('v_forward', 0):+.2f}   "
                 f"v_r {drive.get('v_right', 0):+.2f}   dist —")

        # speed initial value from the bridge
        if not self._speed_init and status.get("ctrl"):
            try:
                self.speed_var.set(float(status["ctrl"].get("vmax", 2.5)))
                self.on_speed_move(self.speed_var.get())
                self._speed_init = True
            except Exception:
                pass

        # target distance readout
        if self.target is None:
            self.target = self.target_from_entries()
        self.update_target_info()

        # mission list
        self.update_mission(status.get("mission") or {})

        # follow
        if self.map.follow and est:
            self.map.center_on(est["lat"], est["lon"])

        # map & camera
        self.map.redraw_overlays(est, self.target, status.get("mission") or {}, drive)
        self.update_camera(snap, seq)

        self.root.after(120, self.refresh)

    def update_mission(self, mission):
        self.mission = mission
        pts = mission.get("points") or []
        active = bool(mission.get("active"))
        idx = mission.get("idx", -1)
        key = (tuple(tuple(round(float(v), 7) for v in p) for p in pts),
               idx, active)
        if key != getattr(self, "_last_mission_key", None):
            self._last_mission_key = key
            self.mission_list.delete(0, "end")
            for i, p in enumerate(pts):
                self.mission_list.insert("end", f"{i + 1:02d}  {p[0]:.7f}  {p[1]:.7f}")
            if active and 0 <= idx < len(pts):
                self.mission_list.selection_set(idx)
                self.mission_list.see(idx)
        if active:
            self.mission_info.configure(
                text=f"RUNNING {idx + 1}/{len(pts)} · dist "
                     f"{(self.drive.get('dist_m') or 0):.1f} m", fg=AMBER)
        else:
            self.mission_info.configure(text=f"idle · {len(pts)} points", fg=MUTED)

    # ---- lifecycle --------------------------------------------
    def quit(self):
        self.poller.stop_evt.set()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


def main():
    ap = argparse.ArgumentParser(description="Jam-Me-DJI tactical PC console")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tiles-dir", required=True,
                    help="tile directory (same one the bridge loaded)")
    args = ap.parse_args()
    app = TacticalApp(BridgeClient(args.host, args.port), args.tiles_dir)
    app.root.mainloop()


if __name__ == "__main__":
    main()
