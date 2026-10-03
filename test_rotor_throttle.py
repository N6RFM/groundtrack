#!/usr/bin/env python3
"""
Standalone test for Rotctld's send-throttle logic (min_move_deg=5.0,
min_interval_s=5.0) - the exact same class from run_passes.py, copied
here so this has zero dependency on schedule.yaml, TLEs, or an actual
pass being active. Connects to your REAL, already-running rotctld and
will physically move the antenna - same as a real pass would.

Runs two phases so you can watch (and read the printed reasoning for)
both trigger conditions on real hardware, right now:

  Phase 1 (fast slew):  az increases 8 deg/sec for 10 seconds - well
                         over the 5 deg threshold, so a send should
                         happen on nearly every one-second tick.
  Phase 2 (slow slew):  az increases 0.5 deg/sec for 15 seconds - well
                         under the threshold, so sends should only
                         happen every ~5 seconds (the time-based
                         trigger), not the movement-based one.

Usage:
    python3 test_rotor_throttle.py
        # reads rot_host/rot_port from satellites.yaml

    python3 test_rotor_throttle.py --host 127.0.0.1 --port 4533
        # or specify directly, bypassing satellites.yaml entirely
"""

import argparse
import socket
import time

import yaml


class Rotctld:
    """Identical to run_passes.py's own class - copied, not imported, so
    this test has no dependency on the rest of that script."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
        self.last = None
        self.last_sent_at = None

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=3)

    def point(self, az_deg, el_deg, min_move_deg=5.0, min_interval_s=5.0):
        now = time.monotonic()
        moved_enough = True
        time_elapsed = True
        daz = delv = 0.0
        if self.last is not None:
            daz = abs(az_deg - self.last[0])
            delv = abs(el_deg - self.last[1])
            moved_enough = daz >= min_move_deg or delv >= min_move_deg
            time_elapsed = self.last_sent_at is None or \
                (now - self.last_sent_at) >= min_interval_s
            if not moved_enough and not time_elapsed:
                print(f"  SKIPPED  az={az_deg:6.1f} el={el_deg:6.1f}  "
                      f"(Δaz={daz:.1f} Δel={delv:.1f}, neither moved "
                      f"{min_move_deg}° nor {min_interval_s}s elapsed)")
                return
        reason = []
        if moved_enough:
            reason.append(f"moved {max(daz, delv):.1f}° >= {min_move_deg}°")
        if time_elapsed and self.last_sent_at is not None:
            reason.append(f"{now - self.last_sent_at:.1f}s >= {min_interval_s}s elapsed")
        if not reason:
            reason = ["first position"]
        try:
            if self.sock is None:
                self._connect()
            self.sock.sendall(f"P {az_deg:.1f} {el_deg:.1f}\n".encode())
            self.sock.settimeout(0.5)
            self.sock.recv(64)
            self.last = (az_deg, el_deg)
            self.last_sent_at = now
            print(f"  SENT     az={az_deg:6.1f} el={el_deg:6.1f}  ({', '.join(reason)})")
        except OSError as e:
            print(f"  ERROR sending to rotctld: {e}")


def main():
    import station
    station.enter()
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

    print(f"Connecting to rotctld at {host}:{port} - this WILL physically "
          f"move the antenna, same as a real pass. Ctrl-C to stop early.\n")
    rot = Rotctld(host, port)

    print("=== Phase 1: fast slew (8 deg/sec az, 10 seconds) ===")
    print("Expect a SEND on nearly every tick - 8 deg/sec is well over "
          "the 5 deg movement threshold.\n")
    az, el = 90.0, 30.0
    for i in range(10):
        rot.point(az, el)
        az += 8.0
        time.sleep(1)

    print("\n=== Phase 2: slow slew (0.5 deg/sec az, 15 seconds) ===")
    print("Expect SKIPPED lines in between, with a SEND only roughly "
          "every 5 seconds (the time-based trigger, not movement).\n")
    for i in range(15):
        rot.point(az, el)
        az += 0.5
        time.sleep(1)

    print("\nDone. If Phase 1 showed a SEND almost every second and Phase 2 "
          "showed SEND only every ~5 seconds with SKIPPED in between, the "
          "throttle is behaving exactly as designed.")


if __name__ == "__main__":
    main()
