#!/usr/bin/env python3
"""
Enable or disable a satellite without deleting its config.

A disabled satellite is skipped everywhere: relay.py won't bind its
ports, run_passes.py and plan_passes.py won't schedule or execute
passes for it, and preflight.py reports it as skipped rather than
checking it. Its full entry stays in satellites.yaml untouched, so
re-enabling it later needs no reconfiguration at all.

Usage:
    python3 toggle_satellite.py --list
    python3 toggle_satellite.py --disable SCIONX
    python3 toggle_satellite.py --enable SCIONX
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
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", metavar="NAME")
    group.add_argument("--disable", metavar="NAME")
    group.add_argument("--list", action="store_true", help="show every satellite's current state")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    sats = cfg.get("satellites", [])

    if args.list:
        for s in sats:
            state = "enabled" if s.get("enabled", True) else "disabled"
            print(f"  {s['name']:20s} {state}")
        return

    name = args.enable or args.disable
    target_state = args.enable is not None

    match = next((s for s in sats if s["name"] == name), None)
    if match is None:
        names = ", ".join(s["name"] for s in sats)
        sys.exit(f"No satellite named {name!r} in {CONFIG_PATH}. Configured: {names}")

    current = match.get("enabled", True)
    if current == target_state:
        print(f"{name} is already {'enabled' if target_state else 'disabled'} - no change.")
        return

    match["enabled"] = target_state
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    print(f"{name}: {'enabled' if current else 'disabled'} -> "
          f"{'enabled' if target_state else 'disabled'}")
    if not target_state:
        print("Note: relay.py needs a restart to stop listening on its ports "
              "if it's currently running.")


if __name__ == "__main__":
    main()
