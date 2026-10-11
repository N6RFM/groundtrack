#!/usr/bin/env python3
"""
Tell the toolkit which SDRs THIS computer has, once, instead of editing templates and fighting git.

Writes sdr.local.yaml in the station folder (never committed): the device string for each SDR
driver. New satellites built by new_record_only_satellite.py use it, and preflight.py warns if a
flowgraph opens a different device.

    python3 set_sdr.py --from flowgraphs/jamx01.grc     # learn it from a flowgraph that works here
    python3 set_sdr.py --set airspy "driver=airspy,soapy=0,serial=10a862dc3464c763"
    python3 set_sdr.py --show                           # what's set, and what each flowgraph uses

Inside a station, pick it as usual (--radio NAME, or GROUNDTRACK_STATION).
"""

import argparse
import glob
import os
import sys

import station
import sdr_local


def main():
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--from", dest="source", metavar="GRC", help="learn this computer's device string from a working flowgraph")
    g.add_argument("--set", nargs=2, metavar=("DRIVER", "STRING"), help="set one driver's device string by hand")
    g.add_argument("--show", action="store_true", help="show what is set and what every flowgraph here uses")
    args = ap.parse_args()

    current = sdr_local.load_local()
    if args.source:
        try:
            with open(args.source, newline="") as f:
                dev = sdr_local.grc_source_args(f.read())
        except OSError as e:
            sys.exit(f"can't read {args.source}: {e}")
        driver = sdr_local.driver_of(dev)
        if not dev or not driver:
            sys.exit(f"{args.source} has no SDR source block with a 'driver=...' device string")
        current[driver] = dev
        sdr_local.save_local(current)
        print(f"{sdr_local.LOCAL_FILE}: {driver} -> {dev}")
    elif args.set:
        driver, dev = args.set
        current[driver] = dev
        sdr_local.save_local(current)
        print(f"{sdr_local.LOCAL_FILE}: {driver} -> {dev}")
    else:
        print(f"{sdr_local.LOCAL_FILE}: " + ("" if current else "(none set - flowgraphs keep whatever they contain)"))
        for k, v in sorted(current.items()):
            print(f"  {k}: {v}")
        print("flowgraphs:")
        for path in sorted(glob.glob("flowgraphs/*.grc")):
            try:
                with open(path, newline="") as f:
                    dev = sdr_local.grc_source_args(f.read())
            except OSError:
                continue
            want = current.get(sdr_local.driver_of(dev) or "")
            mark = "" if not want else ("  ok" if sdr_local.same_device(want, dev) else "  DIFFERENT radio from this computer's setting")
            print(f"  {os.path.basename(path):34s} {dev or '(no SDR source block)'}{mark}")


if __name__ == "__main__":
    main()
