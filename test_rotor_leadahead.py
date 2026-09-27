#!/usr/bin/env python3
"""
Standalone test for the lead-ahead rotor logic (find_lead_ahead_target,
maybe_update_rotor) - imports the REAL functions directly from
run_passes.py, so this tests the actual deployed code, not a
reimplementation that could drift from it. Drives your REAL, already-
running rotctld with synthetic, predictable satellite motion (constant
angular velocity, not a real TLE) so the expected behavior can be
worked out by hand and compared against what actually happens.

This WILL physically move the antenna, same as a real pass. Ctrl-C to
stop early.

Two phases, both starting from a confirmed, settled position (unlike
the earlier throttle-only test, which had a real confound from not
doing this):

  Phase 1 (fast slew): 3 deg/sec azimuth for 20 seconds.
  Phase 2 (slow slew):  0.3 deg/sec azimuth for 75 seconds.

Both use a 5 deg threshold. Real gaps between sends run LONGER than a
naive threshold/speed calculation would suggest: each lead-ahead target
is already sent threshold_deg ahead of the satellite's position at send
time, so the satellite has to catch up to that target first, then drift
another full threshold beyond it, before the next send triggers. A long
quiet stretch in Phase 2 is correct, not a bug - there is no time-based
forced send at all anymore, matching Gpredict_K4KDR_N6RFM's own design.

Usage:
    python3 test_rotor_leadahead.py
        # reads rot_host/rot_port from satellites.yaml

    python3 test_rotor_leadahead.py --host 127.0.0.1 --port 4533
"""

import argparse
import importlib.util
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml


def load_run_passes():
    """Imports the real run_passes.py module by file path, so this test
    uses the actual Rotctld, find_lead_ahead_target, and maybe_update_rotor
    - never a copy that could silently diverge from what's really deployed."""
    spec = importlib.util.spec_from_file_location("run_passes", "run_passes.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    return rp


class FakeTs:
    """Stand-in for skyfield's real timescale object - just enough for
    find_lead_ahead_target's own ts.from_datetime(...) call to work
    against the synthetic motion function below."""
    def from_datetime(self, dt):
        class FakeTime:
            def utc_datetime(self):
                return dt
        return FakeTime()


def make_synthetic_elevation_deg(base_time, start_az, el, deg_per_sec):
    """Returns a replacement for run_passes.elevation_deg - constant
    angular velocity in azimuth, fixed elevation, so the expected
    behavior can be worked out by hand: exactly deg_per_sec degrees of
    azimuth per second of elapsed time from base_time."""
    def fake_elevation_deg(sat, observer, t):
        elapsed_s = (t.utc_datetime() - base_time).total_seconds()
        return el, start_az + deg_per_sec * elapsed_s
    return fake_elevation_deg


def query_rotor_position(host, port):
    """A genuine, independent query - opens its own short-lived connection
    and sends the real rotctld 'p' command (same one used manually earlier
    tonight via `rotctl ... p`), rather than reusing rot's own persistent
    'P'-sending socket. Returns (az, el) as floats, or None on any error."""
    import socket as _socket
    try:
        with _socket.create_connection((host, port), timeout=2) as s:
            s.sendall(b"p\n")
            s.settimeout(1)
            data = s.recv(256).decode(errors="replace")
        parts = data.split()
        return float(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        return None


def run_phase(rp, rot, label, base_time, start_az, el, deg_per_sec,
              duration_s, threshold_deg, host, port):
    print(f"\n=== {label} ===")
    print(f"{deg_per_sec} deg/sec, {duration_s}s total, {threshold_deg} deg "
          f"threshold. Real gaps between sends will run LONGER than "
          f"threshold/speed alone would suggest - each lead-ahead target is "
          f"already sent threshold_deg ahead of the satellite's position at "
          f"send time, so the satellite has to catch up to that target "
          f"first, then drift another full threshold beyond it, before the "
          f"next send triggers. That front-loading is intentional, not a bug.\n")

    rp.elevation_deg = make_synthetic_elevation_deg(base_time, start_az, el, deg_per_sec)
    ts = FakeTs()
    los_dt = base_time + timedelta(seconds=duration_s + 60)  # generous, past this phase's end

    wall_clock_start = time.monotonic()
    for i in range(duration_s):
        # actually wait for real time to pass - this loop previously
        # iterated through simulated seconds almost instantly, sending a
        # whole phase's worth of commands to the real rotor within a
        # fraction of a second. Pacing against wall-clock time (not just
        # sleep(1) each iteration, which would drift) is what makes "every
        # 10 seconds" - and the whole test - mean anything physically real.
        target_wall_time = wall_clock_start + i
        while time.monotonic() < target_wall_time:
            time.sleep(0.05)

        now = base_time + timedelta(seconds=i)
        t_now = ts.from_datetime(now)
        _, cur_az = rp.elevation_deg(None, None, t_now)
        cur_el = el
        before = rot.last
        rp.maybe_update_rotor(rot, None, None, ts, now, cur_az, cur_el,
                               los_dt, threshold_deg=threshold_deg)
        if rot.last != before:
            print(f"  t={i:3d}s  current_az={cur_az:7.2f}  SENT -> "
                  f"az={rot.last[0]:.2f} el={rot.last[1]:.2f}"
                  + ("  (lead-ahead, ahead of current)"
                     if abs(rot.last[0] - cur_az) > 0.05 else "  (first command)"))

        if i % 10 == 0:
            # a genuinely independent query, not a new send and not reusing
            # rot's own persistent socket - compare this printed reading
            # directly against the rotor's own physical digital display
            queried = query_rotor_position(host, port)
            if queried:
                print(f"  t={i:3d}s  [query] rotctld reports rotor at "
                      f"az={queried[0]:.2f} el={queried[1]:.2f} - "
                      f"check this against the rotor's own display now")
            else:
                print(f"  t={i:3d}s  [query] failed to read rotctld's position")
    return base_time + timedelta(seconds=duration_s), start_az + deg_per_sec * duration_s


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    host, port = args.host, args.port
    if host is None or port is None:
        with open("satellites.yaml") as f:
            cfg = yaml.safe_load(f)
        host = host or cfg.get("rot_host", "127.0.0.1")
        port = port or cfg.get("rot_port")
        if port is None:
            raise SystemExit("No --port given and no rot_port in satellites.yaml.")

    rp = load_run_passes()

    print(f"Connecting to rotctld at {host}:{port} - this WILL physically "
          f"move the antenna, same as a real pass. Ctrl-C to stop early.")
    rot = rp.Rotctld(host, port)

    start_az, el = 90.0, 30.0
    print(f"\nSettling at a known starting position first: az={start_az} el={el} ...")
    rot.point(start_az, el)
    time.sleep(5)  # give it time to actually arrive before the real test starts

    base_time = datetime.now(timezone.utc)
    t_end, az_end = run_phase(rp, rot, "Phase 1: fast slew", base_time,
                               start_az, el, deg_per_sec=3.0, duration_s=20,
                               threshold_deg=5.0, host=host, port=port)

    t_end, az_end = run_phase(rp, rot, "Phase 2: slow slew", t_end,
                               az_end, el, deg_per_sec=0.3, duration_s=75,
                               threshold_deg=5.0, host=host, port=port)

    print("\nDone. Phase 1 should have shown several sends, each a genuine "
          "lead-ahead target ahead of the current position at send time. "
          "Phase 2 should have shown far fewer sends, with real gaps between "
          "them - a long quiet stretch here is correct, not a bug. There is "
          "no more time-based forced send in this design at all; the only "
          "trigger is the satellite drifting threshold_deg from wherever "
          "the rotor was last commanded.")


if __name__ == "__main__":
    main()
