#!/usr/bin/env python3
"""
Interactive wizard for the ground-station-level settings in satellites.yaml
(lat/lon/alt, TLE source, shared rig port). Leaves the per-satellite list
untouched - edit those manually in satellites.yaml, or add more with
plan_passes.py's --add-satellite helper (see README).

Run: python3 setup_station.py
"""

import yaml
import os

CONFIG_PATH = "satellites.yaml"


def ask(prompt, default=None, cast=str):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if not raw:
            print("  (required)")
            continue
        try:
            return cast(raw)
        except ValueError:
            print(f"  couldn't parse that as {cast.__name__}, try again")


def main():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}

    print("=== Ground station setup ===")
    gs = cfg.get("ground_station", {})
    gs["lat"] = ask("Latitude (deg, +N)", gs.get("lat"), float)
    gs["lon"] = ask("Longitude (deg, +E)", gs.get("lon"), float)
    gs["alt_m"] = ask("Altitude (m)", gs.get("alt_m", 0), float)
    cfg["ground_station"] = gs

    print("\n=== TLE source ===")
    cfg["tle_file"] = ask("Path to TLE file", cfg.get("tle_file", "tle/amateur.txt"))
    tle_url = ask(
        "Celestrak URL to fetch it from (blank to skip)",
        cfg.get("tle_url", "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle"),
    )
    if tle_url:
        cfg["tle_url"] = tle_url
    print("(If a satellite is ever too new for Celestrak or SatNOGS to have "
          "yet, add 'custom_tle_file: path/to/file.txt' to satellites.yaml "
          "later - update_tle.py never touches it, so a hand-maintained TLE "
          "there is never overwritten. Not needed for this setup.)")

    print("\n=== Doppler control ===")
    cfg["rig_port"] = ask("Shared rigctld port", cfg.get("rig_port", 4532), int)

    sats = cfg.get("satellites", [])
    if sats:
        print(f"\n{len(sats)} satellite(s) already configured, left untouched:")
        for s in sats:
            print(f"  - {s['name']} (NORAD {s['norad']}, {s['freq_hz']} Hz, "
                  f"min elev {s.get('min_elev_deg', '?')} deg)")
        print("Edit satellites.yaml directly to add/remove satellites, "
              "or use plan_passes.py --add-satellite.")
    else:
        cfg["satellites"] = []
        print("\nNo satellites configured yet - add them with "
              "plan_passes.py --add-satellite or by editing satellites.yaml.")

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)
    print(f"\nWrote {CONFIG_PATH}")

    if cfg.get("tle_url"):
        fetch = ask("Fetch TLE file now? (y/n)", "y")
        if fetch.lower().startswith("y"):
            os.makedirs(os.path.dirname(cfg["tle_file"]) or ".", exist_ok=True)
            os.system(f'curl -sL "{cfg["tle_url"]}" -o "{cfg["tle_file"]}"')
            print(f"Fetched TLEs to {cfg['tle_file']}")


if __name__ == "__main__":
    main()
