#!/usr/bin/env python3
"""
Refresh the TLE file used by plan_passes.py/run_passes.py.

SatNOGS is the primary source, Celestrak the fallback - the other way
around from this script's earlier design. SatNOGS's own catalog
includes "temporary" pre-NORAD-catalog designators for recently-launched
satellites (exactly the situation several satellites here were in: too
new for Celestrak/Space-Track's official catalog to have picked them up
yet, but already tracked and given a working ID by SatNOGS). Celestrak
only gets consulted per-satellite, for whatever SatNOGS didn't have -
usually nothing, once a satellite's ID has been fully catalogued.

Every satellite currently in satellites.yaml is covered automatically,
individually, by its own norad value - there's nothing to remember to
list on the command line for a new satellite the way earlier versions
of this script needed (a base-group-plus-manual-catalog-number
approach, which had a real failure mode: forgetting to list a
satellite meant it silently dropped out of the file on the next run).

Validates the result before overwriting the real file - a failed/empty/
error-page download would otherwise silently replace good TLE data with
garbage, breaking pass planning for the whole fleet until someone
happened to notice.

Usage:
    python3 update_tle.py
        # the only normal invocation - covers every configured satellite

    python3 update_tle.py --check-only
        # just report current file's age and satellite coverage, don't download

With radios.yaml (multi-station mode), this covers every station's satellites
in one run and ignores --radio: the stations share a single TLE file, so
refreshing it from only one station's list would drop the others.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import yaml

CONFIG_PATH = "satellites.yaml"
SATNOGS_TLE_URL = "https://db.satnogs.org/api/tle/?format=json"
CELESTRAK_CATNR_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=tle"

# Identify ourselves honestly on every request. Python's default
# "Python-urllib/3.x" agent is commonly blocked by servers (SatNOGS
# did exactly that), and a descriptive agent with a project URL lets
# the operators see who is calling and contact us if needed.
USER_AGENT = "groundtrack/0.1 (+https://github.com/N6RFM/groundtrack)"


def _open(url, timeout):
    """urlopen() with our User-Agent header attached."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def load_cfg():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def fetch_satnogs():
    """One bulk fetch covering everything SatNOGS knows about - there's
    no per-satellite filter that actually works server-side (tested
    directly: ?norad_cat_id= is silently ignored, the full list comes
    back regardless), so filtering down to what's actually configured
    happens locally in Python instead. Returns {norad: (tle0, tle1,
    tle2)}, or an empty dict on any failure."""
    print(f"Downloading SatNOGS TLE catalog ({SATNOGS_TLE_URL}) ...")
    try:
        with _open(SATNOGS_TLE_URL, 30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        entries = json.loads(raw)
    except Exception as e:
        print(f"  WARNING: SatNOGS fetch failed: {e} - falling back to "
              f"Celestrak for every satellite.")
        return {}
    by_norad = {}
    for entry in entries:
        norad = entry.get("norad_cat_id")
        tle0, tle1, tle2 = entry.get("tle0"), entry.get("tle1"), entry.get("tle2")
        if norad is None or not (tle1 and tle2):
            continue
        by_norad[norad] = (tle0 or f"0 NORAD {norad}", tle1, tle2)
    print(f"  SatNOGS: {len(by_norad)} satellite(s) available.")
    return by_norad


def fetch_celestrak_one(norad):
    """Fallback for a single satellite SatNOGS didn't have. Returns
    (tle0, tle1, tle2) or None."""
    url = CELESTRAK_CATNR_URL.format(catnr=norad)
    try:
        with _open(url, 15) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    Celestrak fallback for {norad} failed: {e}")
        return None
    lines = [l.rstrip("\n") for l in data.splitlines() if l.strip()]
    if len(lines) < 3 or not lines[1].startswith("1 ") or not lines[2].startswith("2 "):
        print(f"    Celestrak fallback for {norad}: no usable TLE in response")
        return None
    return lines[0], lines[1], lines[2]


def main():
    # Unlike every other script, this one deliberately does NOT pick a
    # station: stations share one TLE file, and this rebuilds that whole file
    # from whichever satellites it's told about - so reading one station's
    # list alone would drop the others' TLEs the next time it ran. In
    # multi-station mode it reads every station's satellites.yaml instead.
    import station
    multi = station.enter_all()

    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-only", action="store_true",
                     help="just report current file's age and satellite coverage, don't download")
    args = ap.parse_args()

    cfg = station.merged_tle_config() if multi else load_cfg()
    if multi:
        print("Covering every station: " + ", ".join(
            f"{name} ({n} satellite{'s' if n != 1 else ''})"
            for name, n in cfg["_stations"]))
    tle_path = cfg["tle_file"]
    custom_tle_path = cfg.get("custom_tle_file")
    satellites = cfg.get("satellites", [])

    def read_norads(path):
        """NORADs present in one TLE file, or an empty set if it doesn't
        exist - callers decide whether that's notable, not this helper."""
        if not os.path.exists(path):
            return set()
        with open(path) as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
        norads = set()
        for i in range(0, len(lines) - 2, 3):
            if lines[i + 1].startswith("1 "):
                try:
                    norads.add(int(lines[i + 1][2:7]))
                except ValueError:
                    pass
        return norads

    if args.check_only:
        if not os.path.exists(tle_path):
            sys.exit(f"{tle_path} does not exist.")
        age_h = (time.time() - os.path.getmtime(tle_path)) / 3600
        print(f"{tle_path}: {age_h:.1f} hour(s) old")
        present_norads = read_norads(tle_path)
        custom_norads = read_norads(custom_tle_path) if custom_tle_path else set()
        if custom_tle_path:
            label = "exists" if custom_norads or os.path.exists(custom_tle_path) else "not created yet"
            print(f"{custom_tle_path} ({label}): {len(custom_norads)} satellite(s) - "
                  f"never touched by this script")
        all_present = present_norads | custom_norads
        missing = [s for s in satellites if s.get("norad") not in all_present]
        print(f"Current file: {len(present_norads)} satellite(s), "
              f"{len(satellites)} configured in {CONFIG_PATH}")
        if missing:
            print("  MISSING from TLE file (and custom_tle_file, if set):")
            for s in missing:
                print(f"    {s['name']} (norad {s.get('norad')})")
        else:
            print("  all configured satellites present"
                  + (" (via tle_file and/or custom_tle_file)." if custom_tle_path else "."))
        sys.exit(1 if missing else 0)

    satnogs_data = fetch_satnogs()

    lines_out = []
    found_via_satnogs, found_via_celestrak, still_missing = [], [], []

    for sat in satellites:
        norad = sat.get("norad")
        name = sat.get("name", "?")
        if norad is None:
            continue
        if norad in satnogs_data:
            lines_out.extend(satnogs_data[norad])
            found_via_satnogs.append(name)
            continue
        print(f"  {name} (norad {norad}) not in SatNOGS - trying Celestrak fallback ...")
        result = fetch_celestrak_one(norad)
        if result:
            lines_out.extend(result)
            found_via_celestrak.append(name)
        else:
            still_missing.append(name)

    total_found = len(found_via_satnogs) + len(found_via_celestrak)
    if total_found == 0:
        sys.exit(f"No usable TLE data for any configured satellite from either source - "
                  f"refusing to overwrite {tle_path}. The existing file is untouched.")

    combined_text = "\n".join(lines_out) + "\n"

    tle_dir = os.path.dirname(os.path.abspath(tle_path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=tle_dir, prefix=".tle_download_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(combined_text)
        shutil.move(tmp_path, tle_path)
    except Exception:
        os.unlink(tmp_path)
        raise

    print(f"\nWrote {tle_path} ({total_found} satellite(s) total).")
    print(f"  {len(found_via_satnogs)} via SatNOGS, {len(found_via_celestrak)} via "
          f"Celestrak fallback.")
    if still_missing:
        # a satellite not yet in either public source may already have a
        # hand-maintained entry in custom_tle_file - that's the intended
        # fix for exactly this situation, not a problem to warn about
        custom_norads = read_norads(custom_tle_path) if custom_tle_path else set()
        by_name = {s.get("name"): s.get("norad") for s in satellites}
        covered = [name for name in still_missing if by_name.get(name) in custom_norads]
        truly_missing = [name for name in still_missing if name not in covered]
        if covered:
            print(f"\n{len(covered)} satellite(s) not in SatNOGS or Celestrak, but "
                  f"covered by custom_tle_file: {', '.join(covered)}")
        if truly_missing:
            print(f"\nWARNING: {len(truly_missing)} configured satellite(s) not found "
                  f"in either source, and not in custom_tle_file: "
                  f"{', '.join(truly_missing)}. Their pass planning will fail until "
                  f"this is resolved - either source catches up, or add a TLE for "
                  f"{'it' if len(truly_missing) == 1 else 'them'} to "
                  f"{custom_tle_path or 'a custom_tle_file you configure'}.")
            sys.exit(1)


if __name__ == "__main__":
    main()
