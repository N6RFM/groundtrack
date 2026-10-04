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

    default: beam                  # what the GUI opens on
    stations:
      beam:  {dir: beam,  label: "Beam (Az/El)"}
      helix: {dir: helix, label: "Helix (fixed)"}

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

import errno
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


def switch(name):
    """The in-process version of enter(), for a long-running program (the
    GUI) that changes station while it runs. Changes into the station's
    folder and updates GROUNDTRACK_STATION so anything it launches next
    inherits the choice. Raises StationError. Returns the station's real name."""
    global _entered
    _default, stations = load_radios()
    matched = _match(name, stations)
    if matched is None:
        raise StationError(f"unknown station {name!r}. Choose from: {', '.join(stations)}")
    spec = stations[matched]
    if not os.path.isdir(spec["dir"]):
        raise StationError(f"station {matched!r}: its folder {spec['dir']} doesn't exist")
    os.chdir(spec["dir"])
    os.environ[ENV_VAR] = matched
    _entered = matched
    return matched


def initial_station():
    """Which station a program that never prompts (the GUI) should start on:
    --radio, then GROUNDTRACK_STATION, then radios.yaml's own 'default:'.
    Takes --radio off argv. Raises StationError."""
    asked = pop_radio_arg()
    default, stations = load_radios()
    from_env = not asked and bool(os.environ.get(ENV_VAR))
    name = asked or os.environ.get(ENV_VAR) or default
    matched = _match(name, stations)
    if matched is None:
        where = (f" (from {ENV_VAR} in your environment - a stale export after renaming a station? "
                 f"change it or unset it)" if from_env else " (from --radio)" if asked else " (radios.yaml's default:)")
        raise StationError(f"unknown station {name!r}{where}. Choose from: {', '.join(stations)}")
    return matched


def run_passes_pid(name):
    """PID of a live run_passes.py working in that station's folder, or None.
    The same liveness test run_passes.py's own acquire_lock() uses (signal 0;
    EPERM means alive but owned by someone else) - so the lock file, which is
    per-folder and therefore per-station for free, is the single source of
    truth, and a stale lock left by a crash correctly reads as 'not running'."""
    _default, stations = load_radios()
    matched = _match(name, stations)
    if matched is None:
        return None
    try:
        with open(os.path.join(stations[matched]["dir"], "run_passes.lock")) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError as e:
        return pid if e.errno == errno.EPERM else None
    return pid


def describe(name):
    """What the GUI shows about a station, read from the station's own
    satellites.yaml: label, folder, rig_port, rotor ('host:port', or None if
    it has none - the same test run_passes.py applies), and any running PID."""
    _default, stations = load_radios()
    matched = _match(name, stations)
    if matched is None:
        raise StationError(f"unknown station {name!r}")
    spec = stations[matched]
    cfg = {}
    try:
        with open(os.path.join(spec["dir"], "satellites.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        pass
    rotor = (f"{cfg['rot_host']}:{cfg['rot_port']}"
             if "rot_host" in cfg and "rot_port" in cfg else None)
    return {"name": matched, "label": spec["label"], "dir": spec["dir"],
            "rig_port": cfg.get("rig_port"), "rotor": rotor,
            "running_pid": run_passes_pid(matched)}


def _ports_in(cfg):
    """Every relay/bridge/flowgraph TCP port one satellites.yaml claims, as
    [(port, 'SAT key'), ...]."""
    found = []
    for sat in cfg.get("satellites") or []:
        for key in ("producer_port", "consumer_port"):
            if sat.get(key) is not None:
                found.append((sat[key], f"{sat.get('name', '?')} {key}"))
        for out in sat.get("extra_outputs") or []:
            for key in ("port", "bridge_port"):
                if out.get(key) is not None:
                    found.append((out[key], f"{sat.get('name', '?')} {out.get('name', '?')} {key}"))
    return found


def other_stations_ports():
    """Every port claimed by a station OTHER than the one this process is
    working in (all of them if it isn't in one). For 'next free port': each
    station's own satellites.yaml only knows its own ports, so without this a
    new satellite on the helix would be handed 9101 - which the beam station's GEOSCAN-1
    already holds - and the clash would only surface later, as preflight's
    cross-station check failing. Empty set in classic single-folder mode."""
    if not multi_station():
        return set()
    try:
        _default, stations = load_radios()
    except StationError:
        return set()
    used = set()
    for name, spec in stations.items():
        if name == _entered:
            continue
        try:
            with open(os.path.join(spec["dir"], "satellites.yaml")) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        used.update(p for p, _ in _ports_in(cfg) if isinstance(p, int))
    return used


def _all_configs():
    """[(station name or None, config, folder)] for every station's satellites.yaml - or the one in
    the current folder in classic mode. Missing or unreadable files are skipped."""
    out = []
    if multi_station():
        try:
            _default, stations = load_radios()
        except StationError:
            return out
        for name, spec in stations.items():
            try:
                with open(os.path.join(spec["dir"], "satellites.yaml")) as f:
                    out.append((name, yaml.safe_load(f) or {}, spec["dir"]))
            except (OSError, yaml.YAMLError):
                continue
    else:
        try:
            with open("satellites.yaml") as f:
                out.append((None, yaml.safe_load(f) or {}, "."))
        except (OSError, yaml.YAMLError):
            pass
    return out


def configured_ports():
    """Every local TCP port the configuration claims, for doctor.py's port check:
    [(port, label, rig_stations)] sorted by port. Read from every station's config (or the
    one in the current folder), so a station added tomorrow is checked tomorrow - nothing to
    remember here. Each station's rigctld port, its rotctld port if the rotor is local, and
    the relay/bridge/flowgraph ports of its ENABLED satellites. rig_stations names the
    stations whose rigctld that port is (run_passes.py starts it, so it being busy while that
    station's run_passes.py is alive is expected)."""
    found = {}
    def add(port, label, rig_station=None):
        if not isinstance(port, int) or isinstance(port, bool):
            return
        e = found.setdefault(port, {"labels": [], "rig": set()})
        if label not in e["labels"]:
            e["labels"].append(label)
        if rig_station:
            e["rig"].add(rig_station)
    import lanes
    for st, cfg, base in _all_configs():
        pre = f"{st}: " if st else ""
        add(cfg.get("rig_port"), f"{pre}rigctld (Doppler)", st)
        if "rot_host" in cfg and "rot_port" in cfg and cfg["rot_host"] in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
            add(cfg["rot_port"], f"{pre}rotctld (antenna)")
        enabled = [s for s in cfg.get("satellites") or [] if s.get("enabled", True)]
        for s in enabled:       # a second receiver on the same antenna has its own rigctld (its flowgraph says which port)
            lane = lanes.doppler_port(s, cfg.get("rig_port"), base)
            if lane is not None and lane != cfg.get("rig_port"):
                add(lane, f"{pre}rigctld (Doppler, own receiver)", st)
        for port, detail in _ports_in({"satellites": enabled}):
            add(port, f"{pre}{detail}")
    return [(p, " / ".join(e["labels"]), e["rig"]) for p, e in sorted(found.items())]


def configured_scripts():
    """Basenames of the compiled flowgraph scripts of every enabled satellite, in every
    station - what doctor.py looks for among the running processes."""
    names = set()
    for _st, cfg, _base in _all_configs():
        for s in cfg.get("satellites") or []:
            if s.get("enabled", True) and s.get("script"):
                names.add(os.path.basename(str(s["script"])))
    return names


def _norads_in_tle_file(path):
    """NORADs listed in a 3-line TLE file ({} if it's missing or unreadable)."""
    found = set()
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
    except OSError:
        return found
    for i in range(0, len(lines) - 2, 3):
        try:
            found.add(int(lines[i + 1][2:7]))
        except (ValueError, IndexError):
            pass
    return found


def cross_station_norad_clashes():
    """Enabled satellites in different stations that share a NORAD number but carry
    different names. Tracking one satellite on both radios is normal (same number, same
    name - not reported). The same number under two names usually means one of the numbers
    is wrong, and nothing else would notice: each station looks its TLE up on its own, so
    both quietly run - possibly on different orbits. Returns plain-language warnings."""
    _default, stations = load_radios()
    names, source = {}, {}          # norad -> {station: {names}} ; (station, norad) -> where its TLE comes from
    for st, spec in stations.items():
        try:
            with open(os.path.join(spec["dir"], "satellites.yaml")) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        custom = cfg.get("custom_tle_file")
        custom_norads = _norads_in_tle_file(
            custom if custom and os.path.isabs(custom) else os.path.join(spec["dir"], custom)) if custom else set()
        for sat in cfg.get("satellites") or []:
            n = sat.get("norad")
            if n is None or not sat.get("enabled", True):
                continue
            names.setdefault(n, {}).setdefault(st, set()).add(str(sat.get("name", "?")))
            source[(st, n)] = "custom_tle_file" if n in custom_norads else "tle_file"
    warnings = []
    for n, by_station in sorted(names.items()):
        if len(by_station) < 2 or len({nm for s in by_station.values() for nm in s}) < 2:
            continue
        who = "; ".join(f"{', '.join(sorted(nms))} in {st}" for st, nms in by_station.items())
        text = (f"NORAD {n} is {who}. Fine if they're the same satellite; if not, one of the "
                f"numbers is wrong.")
        if len({source[(st, n)] for st in by_station}) > 1:
            text += (" The stations also take its TLE from different places ("
                     + ", ".join(f"{st}: {source[(st, n)]}" for st in by_station)
                     + "), so they are tracking different orbits for this number.")
        warnings.append(text)
    return warnings


def cross_station_conflicts():
    """Things two stations must never share, found by reading every station's
    satellites.yaml: rig_port (two run_passes.py would both try to start
    rigctld on it), a rotor (two stations steering one antenna), and relay /
    bridge ports (two processes can't bind the same one). Returns plain-language
    problems; [] means the stations are properly independent.

    This exists because 'independent' is the whole point of having stations -
    and it's a property of two separate config files, which nothing else is
    positioned to notice."""
    _default, stations = load_radios()
    seen = {}      # what -> {station: detail}
    def note(key, who, detail):
        seen.setdefault(key, {}).setdefault(who, detail)
    for name, spec in stations.items():
        try:
            with open(os.path.join(spec["dir"], "satellites.yaml")) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if cfg.get("rig_port") is not None:
            note(("rig_port", cfg["rig_port"]), name, "rigctld")
        import lanes
        for s in cfg.get("satellites") or []:      # a second receiver's Doppler channel (read from its flowgraph)
            if s.get("enabled", True):
                lane = lanes.doppler_port(s, cfg.get("rig_port"), spec["dir"])
                if lane is not None and lane != cfg.get("rig_port"):
                    note(("rig_port", lane), name, "rigctld (own receiver)")
        if "rot_host" in cfg and "rot_port" in cfg:
            note(("rotor", str(cfg["rot_host"]), cfg["rot_port"]), name, "the rotor")
        for port, detail in _ports_in(cfg):
            note(("port", port), name, detail)
    problems = []
    for key, who in seen.items():
        if len(who) < 2:
            continue
        names = " and ".join(f"{s} ({d})" for s, d in who.items())
        if key[0] == "rig_port":
            problems.append(f"rig_port {key[1]} is used by both {' and '.join(who)} - two "
                            f"run_passes.py would both try to start rigctld on it")
        elif key[0] == "rotor":
            problems.append(f"{' and '.join(who)} both steer the rotor at {key[1]}:{key[2]} - "
                            f"only one station should ever control a given antenna")
        else:
            problems.append(f"port {key[1]} is used by {names}")
    return problems


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
