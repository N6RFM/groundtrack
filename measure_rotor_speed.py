#!/usr/bin/env python3
"""
Empirically measures your rotor's actual max slew rate, in isolation
from any lead-ahead/threshold tracking logic - a single, large,
uninterrupted point-to-point move, timed via real get_pos() polling.
This is the clean, direct measurement the rot_max_deg_per_sec value in
satellites.yaml should actually be based on, not an eyeballed guess from
watching a real pass or a test_rotor_leadahead.py run (which measures
tracking BEHAVIOR, not the rotor's own raw capability in isolation).

Moves through several distances (small, medium, large) so you can see
whether the rotor's rate is roughly constant, or whether accel/decel at
the start and end of a move meaningfully affects shorter moves - useful
context for deciding how conservative a margin to apply.

This WILL physically move the antenna. Ctrl-C to stop early.

Usage:
    python3 measure_rotor_speed.py
        # reads rot_host/rot_port from satellites.yaml

    python3 measure_rotor_speed.py --host 127.0.0.1 --port 4533
    python3 measure_rotor_speed.py --distances 20 60 120
"""

import argparse
import socket
import time

import yaml


class Rotctld:
    """Minimal, standalone client - deliberately not importing from
    run_passes.py, since this tool measures the rotor's raw physical
    capability, independent of anything in the tracking logic it's meant
    to inform."""

    def __init__(self, host, port):
        self.host, self.port = host, port

    def point(self, az_deg, el_deg):
        with socket.create_connection((self.host, self.port), timeout=3) as s:
            s.sendall(f"P {az_deg:.1f} {el_deg:.1f}\n".encode())
            s.settimeout(1)
            s.recv(64)

    def get_pos(self):
        try:
            with socket.create_connection((self.host, self.port), timeout=2) as s:
                s.sendall(b"p\n")
                s.settimeout(1)
                data = s.recv(256).decode(errors="replace")
            parts = data.split()
            return float(parts[0]), float(parts[1])
        except (OSError, ValueError, IndexError):
            return None


def wait_for_arrival(rot, target_az, target_el, tolerance_deg=0.5,
                      assumed_speed_deg_per_sec=2.0, extra_buffer_s=10.0,
                      poll_interval_s=0.25, log_samples=None):
    """Same approach as test_rotor_leadahead.py's own settle step -
    conservative timeout, actual polling for real confirmed arrival, not
    a fixed sleep. If log_samples is given, appends (elapsed_s, az, el)
    for every poll, for the caller to compute an achieved rate from."""
    start_pos = rot.get_pos()
    if start_pos is None:
        raise SystemExit("Couldn't query starting position - is rotctld reachable?")

    distance = max(abs(target_az - start_pos[0]), abs(target_el - start_pos[1]))
    max_wait_s = distance / assumed_speed_deg_per_sec + extra_buffer_s

    rot.point(target_az, target_el)
    start_time = time.monotonic()
    elapsed = 0.0
    while elapsed < max_wait_s:
        time.sleep(poll_interval_s)
        elapsed = time.monotonic() - start_time
        pos = rot.get_pos()
        if pos is None:
            continue
        if log_samples is not None:
            log_samples.append((elapsed, pos[0], pos[1]))
        if abs(pos[0] - target_az) <= tolerance_deg and abs(pos[1] - target_el) <= tolerance_deg:
            return True
    return False


def measure_one_move(rot, from_az, to_az, el):
    """Settles at from_az first (untimed), then times the move to to_az,
    logging samples throughout - returns (achieved_rate_deg_per_sec,
    samples) or (None, samples) if it never confirmed arrival."""
    print(f"  settling at az={from_az} before starting the timed move ...")
    if not wait_for_arrival(rot, from_az, el):
        print("  WARNING: didn't confirm settling - measurement may be off")

    samples = []
    print(f"  timed move: az={from_az} -> az={to_az} ...")
    arrived = wait_for_arrival(rot, to_az, el, log_samples=samples)
    if not samples:
        return None, samples

    # achieved rate = total distance / time to the LAST sample that's
    # still meaningfully moving - using the full first-to-last sample
    # span is simplest and already accounts for any accel/decel at the
    # edges, since those are included in the timing
    total_time = samples[-1][0]
    total_dist = abs(samples[-1][1] - from_az)
    if total_time <= 0:
        return None, samples
    rate = total_dist / total_time
    status = "arrived" if arrived else "TIMED OUT (used partial distance/time)"
    print(f"  {status}: {total_dist:.1f} deg in {total_time:.1f}s = "
          f"{rate:.2f} deg/sec")
    return rate, samples


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--distances", type=float, nargs="+", default=[20.0, 60.0, 120.0],
                     help="azimuth distances (degrees) to test moves over (default: 20 60 120)")
    ap.add_argument("--el", type=float, default=30.0, help="elevation to hold throughout (default: 30)")
    ap.add_argument("--base-az", type=float, default=90.0,
                     help="starting azimuth for the first move (default: 90)")
    args = ap.parse_args()

    host, port = args.host, args.port
    if host is None or port is None:
        with open("satellites.yaml") as f:
            cfg = yaml.safe_load(f)
        host = host or cfg.get("rot_host", "127.0.0.1")
        port = port or cfg.get("rot_port")
        if port is None:
            raise SystemExit("No --port given and no rot_port in satellites.yaml.")

    print(f"Connecting to rotctld at {host}:{port} - this WILL physically "
          f"move the antenna. Ctrl-C to stop early.\n")
    rot = Rotctld(host, port)

    results = []
    az = args.base_az
    for dist in args.distances:
        target = az + dist
        print(f"\n=== Move of {dist:.0f} degrees ===")
        rate, _ = measure_one_move(rot, az, target, args.el)
        if rate is not None:
            results.append((dist, rate))
        az = target  # chain moves so we don't need to backtrack every time

    if not results:
        print("\nNo successful measurements - check rotctld is reachable and "
              "responding to 'p' queries.")
        return

    print("\n=== Summary ===")
    for dist, rate in results:
        print(f"  {dist:5.0f} deg move: {rate:.2f} deg/sec achieved")

    fastest = max(r for _, r in results)
    conservative = fastest * 0.7  # 30% safety margin, not a measured fact - a judgment call
    print(f"\nFastest achieved: {fastest:.2f} deg/sec")
    print(f"Suggested rot_max_deg_per_sec (with a 30% safety margin): "
          f"{conservative:.2f}")
    print(f"\nAdd this to satellites.yaml's top level (alongside rot_host/"
          f"rot_port), not per-satellite - it's a property of the physical "
          f"rotor, not any one satellite:")
    print(f"  rot_max_deg_per_sec: {conservative:.2f}")
    print(f"\nThen python3 plan_passes.py will warn you about any specific "
          f"upcoming pass whose peak angular rate would exceed it.")


if __name__ == "__main__":
    main()
