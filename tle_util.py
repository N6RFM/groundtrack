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
import re

from skyfield.api import EarthSatellite, load

from tle_number import _checksum, catalog_number, encode_catalog_number

CUSTOM_AGE_WARN_DAYS = 14


def fix_six_digit(l1, l2):
    """Lines that write a catalog number of 100000+ as six digits ("1 100933U ...") are one character too
    long, which shifts every later field. Rewrites them in the Alpha-5 form a TLE uses (100933 -> A0933),
    checksums redone. Lines that are not in that state come back unchanged."""
    out = []
    for line in (l1, l2):
        m = re.match(r"^([12]) (\d{6})(.*)$", line)
        if m and len(line) == 70:
            body = f"{m.group(1)} {encode_catalog_number(int(m.group(2)))}{m.group(3)}"[:68]
            line = body + _checksum(body)
        out.append(line)
    return out[0], out[1]


def read_tles(path, wanted_norads, ts):
    """{norad: EarthSatellite} for the wanted NORADs in one 3-line-format TLE file."""
    entries = {}
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    for i in range(0, len(lines), 3):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        l1, l2 = fix_six_digit(l1, l2)
        sat = EarthSatellite(l1, l2, name, ts)
        if sat.model.satnum in wanted_norads:
            entries[sat.model.satnum] = sat
    return entries


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def custom_problems(path, configured):
    """Plain-English problems with a custom_tle_file, so a mistake is named at once instead of the entry
    being silently skipped. `configured` is [(name, norad)] of the satellites it could apply to. [] when
    the file is fine or can't be read. Each message says what is wrong and what to do about it."""
    try:
        with open(path) as f:
            raw = [l.rstrip("\n") for l in f if l.strip()]
    except OSError:
        return []
    by_norad = {n: name for name, n in configured}
    by_name = {_squash(name): (name, n) for name, n in configured}
    out = []
    for i in range(0, len(raw) - 2, 3):
        label, l1, l2 = raw[i].strip(), raw[i + 1].strip(), raw[i + 2].strip()
        if not (l1.startswith("1 ") and l2.startswith("2 ")):
            out.append(f"entry '{label}': its lines don't start with '1 ' and '2 ' - each entry is a name line, then "
                       f"the two TLE lines, with no blank or extra lines between entries")
            continue
        fixed1, fixed2 = fix_six_digit(l1, l2)
        if (fixed1, fixed2) != (l1, l2):
            out.append(f"entry '{label}': the catalog number is written with six digits - a TLE has only five characters "
                       f"for it, so every field after it was one column out. It is read as {fixed1[2:7]} "
                       f"(the Alpha-5 form); to fix the file, write {fixed1[2:7]} in both lines")
            l1, l2 = fixed1, fixed2
        for tag, ln in (("1", l1), ("2", l2)):
            if len(ln) != 69:
                out.append(f"entry '{label}': line {tag} has {len(ln)} characters, a TLE line has exactly 69 - "
                           f"spaces were probably lost or added when it was copied")
            elif ln[-1] != _checksum(ln):
                out.append(f"entry '{label}': line {tag} fails its checksum - a digit was changed or mistyped")
        number = catalog_number(l1)
        if number is None:
            out.append(f"entry '{label}': can't read a catalog number from line 1")
        else:
            hit = by_name.get(_squash(label))
            if hit and hit[1] != number:
                owner = by_norad.get(number)
                out.append(f"entry '{label}' has catalog number {number} ({l1[2:7]})"
                           + (f", which is {owner}'s," if owner else ",")
                           + f" but {hit[0]} is configured as {hit[1]} - the name is never compared, only the number. "
                             f"One of the two is wrong (check for swapped digits); {hit[0]} will not use this entry")
            elif number not in by_norad:
                out.append(f"entry '{label}' has catalog number {number} ({l1[2:7]}), which matches no satellite "
                           f"configured here, so it is not used")
    return out


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
    for msg in custom_problems(custom_path, [(c.get("name", "?"), c["norad"]) for c in cfg.get("satellites", [])
                                             if c.get("enabled", True) and c.get("norad") is not None]):
        print(f"WARNING: custom_tle_file: {msg}")
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
