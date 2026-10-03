#!/usr/bin/env python3
"""
End-to-end downstream test for the whole fleet, in one command. For every
satellite in satellites.yaml:

  - looks up its KISS file sink path from its .grc
  - if a real, non-empty capture exists there, replays real frames from it
  - if not (the common case before any satellite has had a real pass),
    automatically falls back to synthetic frames instead - no test file
    required, no manual per-satellite decision needed

relay.py must already be running. Point your decoder(s) at each
satellite's consumer_port (from satellites.yaml) before running this.

Usage:
    python3 test_downstream.py                 # test every configured satellite
    python3 test_downstream.py --satellite GEOSCAN-1   # just one
    python3 test_downstream.py --count 3 --delay 0.5
"""

import argparse
import os
import yaml

from send_test_frames import (
    kiss_frame, synthetic_ax25_frame, split_kiss_stream, send_frames,
)

CONFIG_PATH = "satellites.yaml"


def decoder_file_for(grc_path):
    try:
        with open(grc_path) as f:
            grc = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError):
        return None
    for b in grc.get("blocks", []):
        if b["id"] == "satellites_kiss_file_sink":
            return b["parameters"].get("file", "").strip("'\"")
    return None


def test_one(sat, count, delay):
    name, port = sat["name"], sat["producer_port"]
    grc_path = sat["script"].replace(".py", ".grc")
    print(f"--- {name} ---")

    kiss_path = decoder_file_for(grc_path)
    frames = None

    if kiss_path and os.path.exists(kiss_path) and os.path.getsize(kiss_path) > 0:
        with open(kiss_path, "rb") as f:
            raw = f.read()
        candidate = split_kiss_stream(raw)[:count]
        if candidate:
            frames = candidate
            print(f"  found real capture ({kiss_path}, {os.path.getsize(kiss_path)} bytes) "
                  f"- replaying {len(frames)} real frame(s)")

    if frames is None:
        if kiss_path:
            reason = ("capture file is empty (no real pass recorded yet)"
                       if os.path.exists(kiss_path)
                       else "no capture file exists yet")
        else:
            reason = "couldn't determine capture file path from .grc"
        print(f"  no usable real capture - {reason}")
        print(f"  falling back to {count} synthetic frame(s) "
              f"(garbage payload - won't decode meaningfully, proves the pipe works)")
        frames = [kiss_frame(synthetic_ax25_frame(i)) for i in range(1, count + 1)]

    try:
        send_frames(port, frames, delay)
    except (ConnectionRefusedError, OSError) as e:
        print(f"  FAILED to connect to relay producer port {port}: {e}")
        print(f"  is relay.py running?")
    print()


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--satellite", help="test only this satellite (default: all)")
    ap.add_argument("--count", type=int, default=5, help="frames per satellite (default 5)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between frames (default 0.5)")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    sats = cfg["satellites"]
    if args.satellite:
        sats = [s for s in sats if s["name"].lower() == args.satellite.lower()]
        if not sats:
            raise SystemExit(f"No satellite named {args.satellite!r} in satellites.yaml")

    print(f"Testing {len(sats)} satellite(s) downstream. "
          f"Make sure relay.py is running and your decoder(s) are connected.\n")
    for sat in sats:
        test_one(sat, args.count, args.delay)

    print("Done. Check each satellite's decoder for new entries in its history pane.")


if __name__ == "__main__":
    main()
