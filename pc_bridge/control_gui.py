#!/usr/bin/env python3
"""
=============================================================
JAM-ME-DJI — PC CONTROL GUI (Tkinter, zero extra deps)
=============================================================
Left:   live drone camera (downward view) via /snapshot
Right:  satellite tile mosaic drawn from the loaded tile_config —
        click anywhere on the map to set the GPS-FREE target
Center: GO / HOLD / STOP controls + live fix + distance status

The drone is piloted exclusively through the bridge's visual fix
(tile localization). No GPS is consumed for decisions.

Open the same bridge the Android app polls:

  python bridge_server.py --source sim            (offline sandbox)
  python bridge_server.py --source rtmp://...     (real Mini 4 Pro)
  python control_gui.py --host 127.0.0.1 --port 8080

Keys: click = select target · GO = drive there · HOLD = hold position
      STOP = idle (zero velocities) · Ctrl+Q = quit
=============================================================
"""

import argparse
import io
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


class BridgeClient:
    def __init__(self, host, port):
        self.base = f"http://{host}:{port}"

    def get_json(self, path, timeout=2.0):
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"error": str(e)[:80]}

    def post_json(self, path, payload, timeout=2.0):
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"ok": False, "error": str(e)[:80]}

    def snapshot_jpeg(self, timeout=2.0):
        try:
            with urllib.request.urlopen(self.base + "/snapshot", timeout=timeout) as r:
                data = r.read()
            if len(data) < 100:
                return None
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            return img
        except Exception:
            return None

    def tile_config(self, timeout=2.0):
        # served via /status extension? keep local fallback
        return None


def load_mosaic(tiles_dir, max_px=760):
    """Compose the tile grid into one map image + geo transform."""
    with open(os.path.join(tiles_dir, "tile_config.json")) as f:
        cfg = json.load(f)
    area, grid = cfg["area"], cfg["grid"]
    tl = area["top_left"]
    span_m = grid["rows"] * grid["tile_size_m"]      # coverage: rows*N, cols*E
    step_m = grid["tile_size_m"] - grid["overlap_m"]
    span_e = grid["cols"] * grid["tile_size_m"]

    scale = max_px / max(span_m, span_e)
    px_w = int(round(span_e * scale))
    px_h = int(round(span_m * scale))
    mosaic = np.full((px_h, px_w, 3), 40, np.uint8)

    coslat = math.cos(math.radians(tl["lat"]))
    for t in cfg["tiles"]:
        c = t["corners"]
        # tile top-left in map pixels
        x0 = (c["top_left"]["lon"] - tl["lon"]) / (span_e / px_w)
        y0 = (tl["lat"] - c["top_left"]["lat"]) / (span_m / px_h)
        img_p = os.path.join(tiles_dir, t["image"])
        tile = cv2.imread(img_p)
        if tile is None:
            continue
        tw = int(round(grid["tile_size_m"] * scale))
        if t["col"] == grid["cols"] - 1 or t["row"] == grid["rows"] - 1:
            tw_r = int(round((span_e - t["col"] * grid["tile_size_m"]) * scale))
            th_r = int(round((span_m - t["row"] * grid["tile_size_m"]) * scale))
        else:
            tw_r, th_r = tw, tw
        crop = cv2.resize(tile, (max(2, min(tw_r, px_w - int(x0))),
                                 max(2, min(th_r, px_h - int(y0)))),
                          interpolation=cv2.INTER_AREA)
        x0i, y0i = max(0, int(x0)), max(0, int(y0))
        mosaic[y0i:y0i + crop.shape[0], x0i:x0i + crop.shape[1]] = crop

    meta = {
        "tl": tl, "span_m": span_m, "span_e": span_e,
        "px_w": px_w, "px_h": px_h, "scale": scale,
        "grid": grid, "cfg": cfg,
    }
    return mosaic, meta


def geo_to_px(lat, lon, meta):
    x = (lon - meta["tl"]["lon"]) / (meta["span_e"] / meta["px_w"])
    y = (meta["tl"]["lat"] - lat) / (meta["span_m"] / meta["px_h"])
    return x, y


class ControlGUI:
    def __init__(self, client, tiles_dir, poll_ms=250):
        self.client = client
        self.poll_ms = poll_ms
        self.mosaic, self.meta = load_mosaic(tiles_dir)
        self.target = None          # (lat, lon) clicked but not issued
        self.status = {"error": "polling…"}
        self.log = []

        self.root = tk.Tk()
        self.root.title("Jam-Me-DJI — PC Control (visual GPS, no GPS required)")
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        # left: live camera
        self.video_label = tk.Label(self.root, bg="black", width=64, height=32)
        self.video_label.grid(row=0, column=0, padx=6, pady=6, sticky="nw")

        # right: map
        self.map_canvas = tk.Canvas(self.root, width=self.meta["px_w"],
                                    height=self.meta["px_h"], bg="black",
                                    highlightthickness=1)
        self.map_canvas.grid(row=0, column=1, padx=6, pady=6, sticky="ne")
        self.map_canvas.bind("<Button-1>", self.on_click)
        self._map_photo = self._tk_image(self.mosaic)
        self.map_canvas.create_image(0, 0, image=self._map_photo, anchor="nw")

        # center controls
        ctrl = tk.Frame(self.root)
        ctrl.grid(row=0, column=2, padx=6, sticky="nsew")
        self.btn_go = tk.Button(ctrl, text="GO  ->  to clicked target",
                                width=26, command=self.issue_go, fg="white", bg="#1a7f37")
        self.btn_hold = tk.Button(ctrl, text="POSITION HOLD  (here)",
                                  width=26, command=self.issue_hold, fg="white", bg="#0b62a4")
        self.btn_stop = tk.Button(ctrl, text="STOP  (zero velocities)",
                                  width=26, command=self.issue_stop, fg="white", bg="#a40b0b")
        self.status_text = tk.Label(ctrl, justify="left", anchor="w", font=("Consolas", 10))
        self.log_text = tk.Label(ctrl, justify="left", anchor="w", font=("Consolas", 8),
                                 fg="#555555")
        for w in (self.btn_go, self.btn_hold, self.btn_stop):
            w.pack(fill="x", pady=2)
        self.status_text.pack(fill="x", pady=6)
        self.log_text.pack(fill="both", pady=4)
        tk.Label(ctrl, text="click map = target · GO = drive\nHOLD = hold current position\nSTOP = zero v\n"
                            "Feedback loop is the VISUAL fix (tiles) — no GPS.",
                 justify="left").pack(fill="x", pady=8)

        self.root.after(self.poll_ms, self.tick)

    # ---- helpers ------------------------------------------------
    def _tk_image(self, img):
        ok, ppm = cv2.imencode(".ppm", img)
        return tk.PhotoImage(data=ppm.tobytes())

    def update_map_overlay(self):
        img = self.mosaic.copy()
        # drone marker
        est = self.status.get("estimated")
        if est:
            x, y = geo_to_px(est["lat"], est["lon"], self.meta)
            xi, yi = int(x), int(y)
            cv2.circle(img, (xi, yi), 9, (40, 200, 60), 2)
            cv2.circle(img, (xi, yi), 3, (40, 200, 60), -1)
        # target marker
        cmd = self.status.get("commanded") or {}
        tgt = cmd.get("target") or self.target
        if tgt:
            x, y = geo_to_px(tgt[0], tgt[1], self.meta)
            cv2.drawMarker(img, (int(x), int(y)), (0, 0, 255), cv2.MARKER_CROSS, 18, 2)
            cv2.putText(img, "TARGET", (int(x) + 10, int(y) - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        self._map_photo = self._tk_image(img)
        self.map_canvas.delete("all")
        self.map_canvas.create_image(0, 0, image=self._map_photo, anchor="nw")

    def update_video(self):
        img = self.client.snapshot_jpeg()
        if img is not None:
            self.video_label.img_tk = self._tk_image(img)
            self.video_label.configure(image=self.video_label.img_tk)
        else:
            self.video_label.configure(text="no video frame yet", image="")

    def on_click(self, event):
        # canvas pixel → geo
        x, y = event.x, event.y
        lon = self.meta["tl"]["lon"] + x * (self.meta["span_e"] / self.meta["px_w"])
        lat = self.meta["tl"]["lat"] - y * (self.meta["span_m"] / self.meta["px_h"])
        self.target = (lat, lon)
        self.log.insert(0, f"[{time.strftime('%H:%M:%S')}] target  lat {lat:.7f}  lon {lon:.7f}")
        self.update_map_overlay()

    def issue_go(self):
        if not self.target:
            self.log.insert(0, "click the map first!")
            return
        r = self.client.post_json("/command", {"action": "go",
                                               "lat": self.target[0], "lon": self.target[1]})
        self.log.insert(0, "GO: " + json.dumps(r)[:120])
        self.log[:] = self.log[:12]

    def issue_hold(self):
        r = self.client.post_json("/command", {"action": "hold"})
        self.log.insert(0, "HOLD: " + json.dumps(r)[:120])
        self.log[:] = self.log[:12]

    def issue_stop(self):
        r = self.client.post_json("/command", {"action": "stop"})
        self.log.insert(0, "STOP: " + json.dumps(r)[:120])
        self.log[:] = self.log[:12]

    def quit(self):
        self.root.destroy()
        os._exit(0)

    # ---- main loop ----------------------------------------------
    def tick(self):
        self.status = self.client.get_json("/status")
        drive = self.client.get_json("/drive")
        est = self.status.get("estimated")
        err = self.status.get("error")
        if err:
            line = f"bridge: {err}"
        elif est:
            line = (f"VISUAL FIX  lat {est['lat']:.7f}  lon {est['lon']:.7f}\n"
                    f"conf {est['conf']:.0%}  matches {est['matches']:<3d}  "
                    f"src {est['src']}  tile {est['tile']}\n"
                    f"fix_valid: {self.status.get('fix_valid')}")
        else:
            line = "waiting for visual fix…"
        dline = ("CMD  mode {mode}  target {target}  "
                 "v_f {v_forward:+.2f} v_r {v_right:+.2f}  "
                 "dist {dist_m} brg {bearing_deg}".format(
                     **{**{"mode": "?", "target": "—", "v_forward": 0, "v_right": 0,
                           "dist_m": "—", "bearing_deg": "—"}, **{k: v for k, v in drive.items()
                                                                  if k in ("mode", "target", "v_forward",
                                                                           "v_right", "dist_m", "bearing_deg")}}))
        self.status_text.configure(text=line + "\n" + dline +
                                        ("   [BLIND]" if drive.get("blind") else ""))
        self.log_text.configure(text="\n".join(self.log[:6]))
        self.update_map_overlay()
        self.update_video()
        self.root.after(self.poll_ms, self.tick)


def main():
    ap = argparse.ArgumentParser(description="Jam-Me-DJI PC control GUI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tiles-dir", required=True)
    args = ap.parse_args()

    gui = ControlGUI(BridgeClient(args.host, args.port), args.tiles_dir)
    gui.root.mainloop()


if __name__ == "__main__":
    main()
