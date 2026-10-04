#!/usr/bin/env python3
"""
Choosing which TLE each satellite uses - the one place that decides, so
plan_passes.py, run_passes.py and preflight.py can never disagree about it.

tle_file is the catalog update_tle.py keeps current. custom_tle_file is yours,
a file you maintain by hand. A NORAD number listed in custom_tle_file ALWAYS
uses your entry - whatever its epoch, and whatever name line it carries. Only
the catalog number on the TLE lines counts, and it has to match the
satellite's norad in your station config; the name is never compared.

That's an override, so it never ages out on its own: if the catalog later gets
newer data your entry keeps winning until you delete it. To keep that from
going unnoticed, every use prints how old the entry is, says so when the
catalog has something newer, and warns past CUSTOM_AGE_WARN_DAYS.
"""

import os

from skyfield.api import EarthSatellite, load

CUSTOM_AGE_WARN_DAYS = 14


def read_tles(path, wanted_norads, ts):
    """{norad: EarthSatellite} for the wanted NORADs in one 3-line-format TLE file."""
    entries = {}
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    for i in range(0, len(lines), 3):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        sat = EarthSatellite(l1, l2, name, ts)
        if sat.model.satnum in wanted_norads:
            entries[sat.model.satnum] = sat
    return entries


def _info(norad, custom_sat, catalog_sat, ts):
    return {
        "norad": norad,
        "epoch": custom_sat.epoch.utc_iso(),
        "age_days": ts.now().tt - custom_sat.epoch.tt,
        "catalog_epoch": catalog_sat.epoch.utc_iso() if catalog_sat else None,
        "catalog_newer": bool(catalog_sat and catalog_sat.epoch.tt > custom_sat.epoch.tt),
    }


def override_lines(info):
    """What to tell the user about one custom entry that's being used: always a
    NOTE with its age, plus a WARNING if it's old or the catalog has newer data."""
    n = info["norad"]
    lines = [f"NOTE: NORAD {n}: using custom_tle_file's TLE (epoch {info['epoch']}, "
             f"{info['age_days']:.1f} days old) - it always takes precedence over tle_file"
             + (f"; tle_file has a newer one (epoch {info['catalog_epoch']}), ignored"
                if info["catalog_newer"] else "") + "."]
    if info["age_days"] > CUSTOM_AGE_WARN_DAYS:
        lines.append(f"WARNING: NORAD {n}'s custom TLE is {info['age_days']:.0f} days old - pointing and "
                     f"Doppler degrade as a TLE ages. Refresh it, or delete the entry to follow the catalog.")
    elif info["catalog_newer"]:
        lines.append(f"WARNING: NORAD {n}: the catalog has newer data than your custom entry. If you'd "
                     f"rather follow it, delete the entry from custom_tle_file.")
    return lines


def load_tles(cfg, wanted_norads):
    """{norad: EarthSatellite}. Entries in custom_tle_file (if configured and present)
    replace the catalog's for the same NORAD, always."""
    ts = load.timescale()
    sats = read_tles(cfg["tle_file"], wanted_norads, ts)
    custom_path = cfg.get("custom_tle_file")
    if not custom_path:
        return sats
    if not os.path.exists(custom_path):
        print(f"NOTE: custom_tle_file is set to {custom_path!r} but that file doesn't exist "
              f"yet - continuing without it.")
        return sats
    for norad, custom_sat in sorted(read_tles(custom_path, wanted_norads, ts).items()):
        for line in override_lines(_info(norad, custom_sat, sats.get(norad), ts)):
            print(line)
        sats[norad] = custom_sat
    return sats


def custom_overrides(cfg, wanted_norads):
    """For preflight: one info dict per wanted NORAD that custom_tle_file overrides,
    without printing anything. [] if there's no custom file."""
    custom_path = cfg.get("custom_tle_file")
    if not custom_path or not os.path.exists(custom_path):
        return []
    ts = load.timescale()
    catalog = read_tles(cfg["tle_file"], wanted_norads, ts) if os.path.exists(cfg["tle_file"]) else {}
    return [_info(n, c, catalog.get(n), ts)
            for n, c in sorted(read_tles(custom_path, wanted_norads, ts).items())]
