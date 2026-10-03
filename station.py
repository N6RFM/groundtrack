#!/usr/bin/env python3
"""
Station selection, for a ground station with more than one SDR/antenna
system running independently (e.g. an Airspy R2 on a steerable beam, and an
Airspy Mini on a fixed helix).

Each station is a complete, self-contained folder - its own satellites.yaml,
flowgraphs/, schedule.yaml, pass_log.jsonl and run_passes.lock - and the
scripts themselves live once, in the project root, never duplicated. A
script picks its station, changes into that folder, and from then on runs
exactly as it always has: every relative path it already uses simply
resolves inside the station instead of the project root. That's the whole
mechanism - which is also why two stations can't tangle: nothing they
touch is shared except the TLE file (see update_tle.py).

radios.yaml, next to this file, is what turns it on:

    default: r2                  # what the GUI opens on
    stations:
      r2:   {dir: r2,   label: "R2 + beam (Az/El)"}
      mini: {dir: mini, label: "Mini + helix (fixed)"}

With no radios.yaml, enter() does nothing at all and every script behaves
exactly as before - the classic single-folder layout.

Choosing a station, in order: --radio NAME on the command line, then the
GROUNDTRACK_STATION environment variable (which is how the GUI hands its
choice to everything it launches), then - only at a real terminal - being
asked. Never a silent default: starting the wrong radio's tracking is the
kind of mistake worth one extra keystroke.

    python3 station.py --list            # what's configured
    python3 station.py --shell [--radio NAME]
        # prints export/cd lines for a shell script to eval (regen_all.sh)
"""

import os
import shlex
import sys

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RADIOS_PATH = os.path.join(SCRIPT_DIR, "radios.yaml")
ENV_VAR = "GROUNDTRACK_STATION"

_entered = None


class StationError(Exception):
    """Something wrong with radios.yaml, or with the station asked for.
    Scripts turn it into a plain exit message; the GUI shows it in a dialog."""


def multi_station():
    """True if radios.yaml exists - i.e. multi-station mode is switched on."""
    return os.path.exists(RADIOS_PATH)


def script_path(name):
    """Absolute path to another script in the project root - for launching
    one from a script that has already changed into a station folder, where
    a bare 'add_satellite.py' would no longer be found."""
    return os.path.join(SCRIPT_DIR, name)


def current():
    """The station this process is working in, or None (classic mode)."""
    return _entered


def _match(text, stations):
    """Case-insensitive lookup; returns the station's real name or None."""
    for name in stations:
        if name.lower() == str(text).strip().lower():
            return name
    return None


def load_radios():
    """-> (default_name, {name: {"dir": absolute path, "label": text}}),
    stations in file order."""
    try:
        with open(RADIOS_PATH) as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise StationError(f"radios.yaml isn't valid YAML: {e}")
    section = raw.get("stations") if isinstance(raw, dict) else None
    if not isinstance(section, dict) or not section:
        raise StationError("radios.yaml needs a 'stations:' section listing at "
                           "least one station (see radios.example.yaml)")

    stations, seen = {}, {}
    for name, spec in section.items():
        name = str(name)
        if not isinstance(spec, dict) or not spec.get("dir"):
            raise StationError(f"radios.yaml: station {name!r} needs a 'dir:'")
        if name.lower() in seen:
            raise StationError(f"radios.yaml: stations {seen[name.lower()]!r} and "
                               f"{name!r} differ only by case")
        seen[name.lower()] = name
        d = os.path.expanduser(str(spec["dir"]))
        if not os.path.isabs(d):
            d = os.path.join(SCRIPT_DIR, d)
        stations[name] = {"dir": os.path.normpath(d),
                          "label": str(spec.get("label") or name)}

    default = raw.get("default")
    if default is None:
        default = next(iter(stations))
    else:
        matched = _match(default, stations)
        if matched is None:
            raise StationError(f"radios.yaml: default {default!r} isn't one of "
                               f"the stations ({', '.join(stations)})")
        default = matched
    return default, stations


def pop_radio_arg(argv=None):
    """Removes --radio NAME / --radio=NAME from argv - so each script's own
    argparse never has to know about it - and returns NAME (None if absent)."""
    argv = sys.argv if argv is None else argv
    name = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--radio":
            if i + 1 >= len(argv):
                raise StationError("--radio needs a station name")
            name = argv[i + 1]
            del argv[i:i + 2]
        elif a.startswith("--radio="):
            name = a.split("=", 1)[1]
            del argv[i]
        else:
            i += 1
    return name


def _prompt(stations):
    if not sys.stdin.isatty():
        raise StationError(
            "no station selected, and there's no terminal to ask on. Use "
            f"--radio NAME or set {ENV_VAR}=NAME. Stations: {', '.join(stations)}")
    # stderr, not stdout: --shell mode captures stdout
    sys.stderr.write("Which station?\n")
    for name, spec in stations.items():
        sys.stderr.write(f"  {name:<8} {spec['label']}\n")
    while True:
        sys.stderr.write("Station: ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if line == "":
            raise StationError("no station chosen (end of input)")
        matched = _match(line, stations)
        if matched:
            return matched
        sys.stderr.write(f"  not one of: {', '.join(stations)}\n")


def _resolve(asked):
    """Works out which station, validates it, returns (name, spec)."""
    default, stations = load_radios()
    source = "--radio"
    name = asked
    if name is None and os.environ.get(ENV_VAR):
        name, source = os.environ[ENV_VAR], ENV_VAR
    if name is None:
        name, source = _prompt(stations), "prompt"
    matched = _match(name, stations)
    if matched is None:
        raise StationError(f"unknown station {name!r} (from {source}). "
                           f"Choose from: {', '.join(stations)}")
    spec = stations[matched]
    if not os.path.isdir(spec["dir"]):
        raise StationError(f"station {matched!r}: its folder {spec['dir']} "
                           f"doesn't exist")
    return matched, spec


def _enter():
    global _entered
    asked = pop_radio_arg()
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        return None  # reading --help shouldn't require choosing a station
    if not multi_station():
        if asked:
            raise StationError("--radio was given, but there's no radios.yaml - "
                               "so there are no stations to choose between "
                               "(see radios.example.yaml)")
        return None
    name, spec = _resolve(asked)
    os.chdir(spec["dir"])
    os.environ[ENV_VAR] = name  # children (spawned scripts, doctor -> preflight) inherit it
    _entered = name
    print(f"[station {name}] {spec['label']} - {spec['dir']}", file=sys.stderr)
    return name


def enter():
    """Call first thing in a script's main(). Picks the station and changes
    into its folder; does nothing in classic single-folder mode. Idempotent.
    Returns the station name, or None."""
    if _entered is not None:
        return _entered
    try:
        return _enter()
    except StationError as e:
        sys.exit(f"station: {e}")


def enter_all():
    """For the one script that deliberately spans every station
    (update_tle.py): takes --radio off the command line without acting on it,
    never changes directory. Returns True in multi-station mode."""
    try:
        asked = pop_radio_arg()
        if not multi_station():
            if asked:
                raise StationError("--radio was given, but there's no radios.yaml")
            return False
    except StationError as e:
        sys.exit(f"station: {e}")
    if asked:
        print("note: update_tle.py covers every station (they share one TLE "
              "file), so --radio is ignored here.", file=sys.stderr)
    return True


def merged_tle_config():
    """What update_tle.py needs from every station at once, as one config:
    a single tle_file (absolute path), an optional custom_tle_file, and the
    union of every station's satellites. The stations share one TLE file, and
    update_tle.py rebuilds that whole file from whichever satellites it's
    told about - so reading only one station's list would silently drop the
    other's TLEs the next time it ran.

    Refuses (rather than guess) if stations point at different TLE files:
    that would mean two files to keep current, which is exactly the
    arrangement this exists to avoid."""
    try:
        _default, stations = load_radios()
    except StationError as e:
        sys.exit(f"station: {e}")

    parts = []
    for name, spec in stations.items():
        path = os.path.join(spec["dir"], "satellites.yaml")
        if not os.path.exists(path):
            print(f"note: station {name!r} has no satellites.yaml yet - "
                  f"skipping it for the TLE refresh.", file=sys.stderr)
            continue
        with open(path) as f:
            parts.append((name, spec["dir"], yaml.safe_load(f) or {}))
    if not parts:
        sys.exit("station: no station has a satellites.yaml - nothing to refresh.")

    def resolve(base, p):
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))

    def agree(key, required):
        found = {}
        for name, d, cfg in parts:
            if cfg.get(key):
                found.setdefault(resolve(d, cfg[key]), []).append(name)
            elif required:
                sys.exit(f"station: station {name!r}'s satellites.yaml has no {key}.")
        if len(found) > 1:
            detail = "; ".join(f"{p} (stations: {', '.join(n)})" for p, n in found.items())
            sys.exit(f"station: stations disagree about {key}: {detail}. They "
                     f"share one TLE file, so they must all point at the same one.")
        return next(iter(found), None)

    merged = dict(parts[0][2])
    merged["tle_file"] = agree("tle_file", required=True)
    custom = agree("custom_tle_file", required=False)
    if custom:
        merged["custom_tle_file"] = custom
    else:
        merged.pop("custom_tle_file", None)

    sats, by_station = {}, []
    for name, _d, cfg in parts:
        listed = cfg.get("satellites") or []
        by_station.append((name, len(listed)))
        for s in listed:
            sats.setdefault(s.get("norad"), s)  # same NORAD on two radios: fetched once
    merged["satellites"] = list(sats.values())
    merged["_stations"] = by_station
    return merged


def _cli(argv):
    if "--list" in argv:
        if not multi_station():
            print("No radios.yaml - classic single-folder mode.")
            return 0
        try:
            default, stations = load_radios()
        except StationError as e:
            print(f"station: {e}", file=sys.stderr)
            return 1
        for name, spec in stations.items():
            mark = "  (default)" if name == default else ""
            gone = "" if os.path.isdir(spec["dir"]) else "   ** folder missing **"
            print(f"{name:<8} {spec['label']}  -  {spec['dir']}{mark}{gone}")
        return 0
    if "--shell" in argv:
        try:
            name = enter()  # sys.exit()s with a message on any problem
        except SystemExit as e:
            print(e, file=sys.stderr)
            return 1
        if name:
            print(f"export {ENV_VAR}={shlex.quote(name)}")
            print(f"cd {shlex.quote(os.getcwd())}")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
