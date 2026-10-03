#!/usr/bin/env python3
"""
Searches a directory tree for each configured satellite's decoder .yml
file (matched by NORAD ID or satellite name appearing in the filename),
and patches the 'file' parameter of that satellite's
satellites_satellite_decoder block in its .grc to point there.

Usage:
    python3 locate_decoders.py ~/Launches
    python3 locate_decoders.py ~/Launches --dry-run   # show matches, don't edit files
"""

import argparse
import os
import re
import sys
import yaml

CONFIG_PATH = "satellites.yaml"


def find_candidates(search_root, sat_name, norad):
    """Return all .yml files under search_root whose name plausibly matches
    this satellite, by NORAD ID or by the satellite's name/number."""
    name_digits = re.search(r"(\d+)", sat_name)
    name_num = name_digits.group(1) if name_digits else None

    candidates = []
    for dirpath, _, filenames in os.walk(os.path.expanduser(search_root)):
        for fn in filenames:
            if not fn.lower().endswith((".yml", ".yaml")):
                continue
            full = os.path.join(dirpath, fn)
            low = fn.lower()
            if str(norad) in fn:
                candidates.append((full, "norad match"))
            elif name_num and re.search(rf"geoscan[-_]?0*{name_num}\b", low):
                candidates.append((full, "name match"))
    return candidates


def patch_grc(grc_path, new_file_path):
    with open(grc_path) as f:
        data = yaml.safe_load(f)
    changed = False
    for b in data["blocks"]:
        if b["id"] == "satellites_satellite_decoder":
            old = b["parameters"].get("file")
            b["parameters"]["file"] = new_file_path
            changed = old != new_file_path
    if changed:
        with open(grc_path, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)
    return changed


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser()
    ap.add_argument("search_root", help="directory to search, e.g. ~/Launches")
    ap.add_argument("--dry-run", action="store_true", help="show matches without editing .grc files")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    for sat in cfg["satellites"]:
        name, norad, script = sat["name"], sat["norad"], sat["script"]
        grc_path = script.replace(".py", ".grc")
        print(f"--- {name} (NORAD {norad}) ---")

        matches = find_candidates(args.search_root, name, norad)
        if not matches:
            print(f"  no candidate .yml found under {args.search_root}")
            continue

        if len(matches) > 1:
            print(f"  {len(matches)} possible matches found - pick one manually:")
            for path, reason in matches:
                print(f"    {path}  ({reason})")
            print(f"  (not auto-patching {grc_path} - ambiguous)")
            continue

        path, reason = matches[0]
        print(f"  found: {path} ({reason})")
        if args.dry_run:
            print(f"  --dry-run: would set {grc_path}'s decoder file to this path")
        elif not os.path.exists(grc_path):
            print(f"  {grc_path} doesn't exist - skipping")
        else:
            changed = patch_grc(grc_path, path)
            print(f"  {'updated' if changed else 'already correct'}: {grc_path}")
            if changed:
                print(f"  remember to run: grcc {grc_path}")


if __name__ == "__main__":
    main()
