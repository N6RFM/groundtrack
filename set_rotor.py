#!/usr/bin/env python3
"""
Set how often and how far the antenna rotor moves while tracking, so it is not told to move constantly.

    python3 set_rotor.py --show                        # what is in effect now
    python3 set_rotor.py --step-deg 5 --min-interval 5 # move only after 5 degrees of drift AND at most every 5 seconds
    python3 set_rotor.py --az-deg 8 --el-deg 4         # different steps for azimuth and elevation
    python3 set_rotor.py --min-interval 0              # no time limit (the default)

Writes rot_threshold_deg / rot_threshold_az_deg / rot_threshold_el_deg / rot_min_interval_s at the top of this
station's satellites.yaml and leaves every other line and comment alone. A running run_passes.py keeps the values it
started with; the next start uses the new ones. (run_passes.py --rot-threshold-deg / --rot-min-interval try values for
one run without saving them.) Inside a station, pick it as usual (--radio NAME, or GROUNDTRACK_STATION).
"""
import argparse
import sys

import yaml

import rotor_settings as rs
import station

CONFIG_PATH = "satellites.yaml"


def describe(cfg):
    e = rs.effective(cfg)
    steps = f"{e['az']:g} deg" if e["az"] == e["el"] else f"azimuth {e['az']:g} deg, elevation {e['el']:g} deg"
    when = f"and at most once every {e['interval']:g} s" if e["interval"] > 0 else "with no time limit between commands"
    return f"The rotor moves only after the satellite drifts {steps}, {when}."


def main():
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true", help="print what is in effect and change nothing")
    ap.add_argument("--step-deg", type=float, metavar="DEG", help="drift needed before a move, both axes")
    ap.add_argument("--az-deg", type=float, metavar="DEG", help="drift needed before a move, azimuth only")
    ap.add_argument("--el-deg", type=float, metavar="DEG", help="drift needed before a move, elevation only")
    ap.add_argument("--min-interval", type=float, metavar="SECONDS", help="never send a new command sooner than this (0 = no limit)")
    args = ap.parse_args()

    try:
        cfg = yaml.safe_load(open(CONFIG_PATH)) or {}
    except (OSError, yaml.YAMLError) as e:
        sys.exit(f"can't read {CONFIG_PATH}: {e}")
    if "rot_host" not in cfg or "rot_port" not in cfg:
        print("Note: this station has no rotor (no rot_host/rot_port), so these settings do nothing here.")
    if args.show or all(v is None for v in (args.step_deg, args.az_deg, args.el_deg, args.min_interval)):
        print(describe(cfg))
        return
    cur = rs.effective(cfg)
    az = args.az_deg if args.az_deg is not None else args.step_deg if args.step_deg is not None else cur["az"]
    el = args.el_deg if args.el_deg is not None else args.step_deg if args.step_deg is not None else cur["el"]
    interval = args.min_interval if args.min_interval is not None else cur["interval"]
    bad = rs.problem(az, el, interval)
    if bad:
        sys.exit(bad)
    try:
        rs.set_keys(CONFIG_PATH, rs.changes_for(az, el, interval))
    except (OSError, ValueError, yaml.YAMLError) as e:
        sys.exit(str(e))
    print(describe(yaml.safe_load(open(CONFIG_PATH))))
    print("Takes effect the next time run_passes.py starts.")


if __name__ == "__main__":
    main()
