#!/usr/bin/env python3
"""
Remove a satellite's entry from satellites.yaml.

Deliberately does NOT delete its .grc/.py/decoder files - those are left
on disk in case you want them back, or want to reference them for a
similar satellite later. Only the config entry goes away. If you also
want the files gone, remove flowgraphs/<name>.grc and .py yourself.

Usage:
    python3 delete_satellite.py --list
    python3 delete_satellite.py SCIONX
    python3 delete_satellite.py SCIONX --yes    # skip the confirmation prompt
"""

import argparse
import sys
import yaml

CONFIG_PATH = "satellites.yaml"


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="satellite name to remove")
    ap.add_argument("--list", action="store_true", help="show every configured satellite")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    sats = cfg.get("satellites", [])

    if args.list:
        for s in sats:
            print(f"  {s['name']}")
        return

    if not args.name:
        sys.exit("Need a satellite name, or --list to see what's configured.")

    match = next((s for s in sats if s["name"] == args.name), None)
    if match is None:
        names = ", ".join(s["name"] for s in sats)
        sys.exit(f"No satellite named {args.name!r} in {CONFIG_PATH}. Configured: {names}")

    if not args.yes:
        print(f"About to remove this entry from {CONFIG_PATH}:")
        print(yaml.dump(match, sort_keys=False, default_flow_style=False))
        print(f"Its .grc/.py files (if any) are NOT touched by this - only the config entry.")
        confirm = input(f"Remove {args.name}? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled, nothing changed.")
            return

    cfg["satellites"] = [s for s in sats if s["name"] != args.name]
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    print(f"Removed {args.name} from {CONFIG_PATH}.")
    print(f"Note: run plan_passes.py to drop any of its passes still sitting in "
          f"schedule.yaml, since it fully regenerates that file from what's "
          f"currently configured.")


if __name__ == "__main__":
    main()
