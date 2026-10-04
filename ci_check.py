#!/usr/bin/env python3
"""
CI-safe validation: the subset of preflight.py's checks that can run on a
bare CI runner with no GNU Radio, no Hamlib, and no real TLE file.
Catches exactly the class of bugs that have actually shipped before:
freq/nfreq mismatches between satellites.example.yaml and a .grc,
network_socket_pdu reverting to TCP_SERVER, port collisions, invalid
YAML, and Python syntax errors - all before anyone ever runs anything.

Usage:
    python3 ci_check.py
"""

import ast
import glob
import os
import py_compile
import re
import sys
import yaml

FAIL, PASS = "FAIL", "PASS"
results = []


def check(name, ok, detail=""):
    results.append((PASS if ok else FAIL, name, detail))
    marker = " ok " if ok else "FAIL"
    line = f"[{marker}] {name}"
    if detail:
        line += f" - {detail}"
    print(line)


def check_python_syntax():
    print("=== Python syntax ===")
    for path in sorted(glob.glob("*.py")):
        try:
            py_compile.compile(path, doraise=True)
            check(f"{path} compiles", True)
        except py_compile.PyCompileError as e:
            check(f"{path} compiles", False, str(e))


# Scripts that mention station-relative files but deliberately don't call
# station.enter() themselves, and why. Anything else that touches
# satellites.yaml / schedule.yaml / flowgraphs/ without choosing a station
# would, in multi-station mode, look in the project root and find nothing.
STATION_EXEMPT = {
    "station.py": "defines station selection",
    "ci_check.py": "checks the repo's own example files, not a station's",
    "migrate_to_stations.py": "runs once, from the project root, before any station exists",
}
STATION_STATE = re.compile(r"satellites\.yaml|schedule\.yaml|flowgraphs/|"
                           r"run_passes\.lock|pass_log\.jsonl|CONFIG_PATH|SCHEDULE_PATH")


# A script launching another by bare name ([sys.executable, "add_satellite.py"])
# stops working the moment it has changed into a station folder, where that
# file doesn't exist. station.script_path("add_satellite.py") works from anywhere.
def bare_launches(src):
    """Line numbers where a command list starts with sys.executable and its
    script is a bare filename - written literally ([sys.executable, "x.py"]) or
    through a name that's assigned one (tool = "x.py" ... [sys.executable, tool]).
    Parsed rather than pattern-matched, so comments and docstrings can't trip it.
    Deliberately not flagged: station.script_path(...) (the right way), and
    anything computed at run time (a satellite's own script: path is meant to be
    relative to its station). It can't see a filename built dynamically."""
    tree = ast.parse(src)
    py_literals = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str) and n.value.value.endswith(".py")):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    py_literals[t.id] = n.value.value
    hits = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.List) and n.elts):
            continue
        head = n.elts[0]
        if not (isinstance(head, ast.Attribute) and head.attr == "executable"
                and isinstance(head.value, ast.Name) and head.value.id == "sys"):
            continue
        rest = n.elts[1:]
        if rest and isinstance(rest[0], ast.Constant) and rest[0].value == "-u":
            rest = rest[1:]
        if not rest:
            continue
        first = rest[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.endswith(".py"):
            hits.append(first.lineno)
        elif isinstance(first, ast.Name) and first.id in py_literals:
            hits.append(first.lineno)
    return hits


def find_grc(path):
    """The example satellites' flowgraphs live in flowgraphs/ in a classic
    layout, or in <station>/flowgraphs/ once migrated to multi-station mode
    (radios.yaml itself is gitignored, so CI can't consult it - look in both)."""
    for candidate in [path] + sorted(glob.glob(f"*/{path}")):
        if os.path.exists(candidate):
            return candidate
    return path


def check_station_wiring():
    print("\n=== multi-station wiring ===")
    for path in sorted(glob.glob("*.py")):
        if path in STATION_EXEMPT:
            continue
        with open(path) as f:
            src = f.read()
        if not STATION_STATE.search(src):
            continue
        # enter(): picks a station at start-up. enter_all(): spans every one
        # (update_tle.py). switch(): changes station while running (the GUI).
        wired = any(call in src for call in
                    ("station.enter()", "station.enter_all()", "station.switch("))
        check(f"{path} chooses a station", wired,
              "" if wired else "reads station files (satellites.yaml, flowgraphs/, ...) "
              "but never calls station.enter() - in multi-station mode it would look "
              "in the project root and find nothing. Add `import station` and "
              "`station.enter()` as the first lines of main(), or list it in "
              "STATION_EXEMPT with the reason.")

    offenders = []
    for path in sorted(glob.glob("*.py")):
        with open(path) as f:
            try:
                lines = bare_launches(f.read())
            except SyntaxError:
                continue   # reported by the compile check above
        if lines:
            offenders.append(f"{path}:{','.join(map(str, lines))}")
    check("no script launches another by bare filename", not offenders,
          f"{', '.join(offenders)} - use station.script_path(...), a bare name isn't "
          f"found once the script has changed into a station folder" if offenders else "")

    try:
        import station
        station.RADIOS_PATH = os.path.abspath("radios.example.yaml")
        default, stations = station.load_radios()
        check("radios.example.yaml is a valid station list", True,
              f"{len(stations)} stations, default {default!r}")
    except Exception as e:  # missing file, bad YAML, or a StationError
        check("radios.example.yaml is a valid station list", False, str(e))


def check_config():
    print("\n=== satellites.example.yaml + .grc cross-checks ===")
    try:
        with open("satellites.example.yaml") as f:
            cfg = yaml.safe_load(f)
        check("satellites.example.yaml parses as YAML", True)
    except (FileNotFoundError, yaml.YAMLError) as e:
        check("satellites.example.yaml parses as YAML", False, str(e))
        return

    all_norads, all_ports = set(), {}
    for sat in cfg.get("satellites", []):
        name = sat.get("name", "<unnamed>")
        print(f"--- {name} ---")

        freq_ok = isinstance(sat.get("freq_hz"), (int, float))
        check(f"{name}.freq_hz is numeric", freq_ok, f"got {sat.get('freq_hz')!r}")

        norad = sat.get("norad")
        if norad is not None:
            check(f"{name}.norad ({norad}) is unique", norad not in all_norads)
            all_norads.add(norad)

        for port_field in ("producer_port", "consumer_port"):
            p = sat.get(port_field)
            if p is not None:
                dup = p in all_ports
                check(f"{name}.{port_field} ({p}) is unique across fleet",
                      not dup, "" if not dup else f"also used by {all_ports.get(p)}")
                all_ports[p] = f"{name}.{port_field}"

        grc_path = find_grc(sat.get("script", "").replace(".py", ".grc"))
        try:
            with open(grc_path) as f:
                grc = yaml.safe_load(f)
            check(f"{grc_path} parses as YAML", True)
        except (FileNotFoundError, yaml.YAMLError) as e:
            check(f"{grc_path} parses as YAML", False, str(e))
            continue

        blocks = {b["name"]: b for b in grc.get("blocks", [])}
        for var in ("freq", "nfreq"):
            if var in blocks:
                grc_val = blocks[var]["parameters"].get("value")
                try:
                    match = int(grc_val) == int(sat["freq_hz"])
                except (TypeError, ValueError):
                    match = False
                check(f"{name}: {var} in .grc matches example freq_hz",
                      match, f".grc={grc_val}  yaml={sat.get('freq_hz')}")

        sock_block = blocks.get("network_socket_pdu_0")
        if sock_block:
            sock_type = sock_block["parameters"].get("type", "")
            check(f"{name}: network_socket_pdu type is TCP_CLIENT",
                  "TCP_CLIENT" in str(sock_type), f"got {sock_type!r}")
        else:
            check(f"{name}: has a network_socket_pdu block", False)


def main():
    check_python_syntax()
    check_station_wiring()
    check_config()
    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    n_pass = sum(1 for s, _, _ in results if s == PASS)
    print(f"\n{n_pass} passed, {n_fail} failed.")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
