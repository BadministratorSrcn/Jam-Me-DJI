#!/usr/bin/env python3
"""Open the Google Maps JavaScript (WebView2) window for JAM-ME.

The bridge must be running and must have GOOGLE_MAPS_API_KEY in its
environment; the bridge serves /map with the key injected, so the key is
never written into a repository file.
"""

import argparse

import webview


def main():
    ap = argparse.ArgumentParser(description="JAM-ME Google Maps window")
    ap.add_argument("--port", type=int, default=8080,
                    help="ground bridge HTTP port")
    ap.add_argument("--width", type=int, default=1100)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--x", type=int, default=80)
    ap.add_argument("--y", type=int, default=80)
    args = ap.parse_args()

    webview.create_window(
        "JAM-ME — Google Maps (satellite)",
        f"http://127.0.0.1:{args.port}/map",
        width=args.width,
        height=args.height,
        x=args.x,
        y=args.y,
    )
    webview.start()


if __name__ == "__main__":
    main()
