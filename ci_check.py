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
    "groundtrack_gui.py": "not station-aware yet - being done as its own step "
                          "(it needs a runtime switch, not a start-up choice)",
}
STATION_STATE = re.compile(r"satellites\.yaml|schedule\.yaml|flowgraphs/|"
                           r"run_passes\.lock|pass_log\.jsonl|CONFIG_PATH|SCHEDULE_PATH")


def check_station_wiring():
    print("\n=== multi-station wiring ===")
    for path in sorted(glob.glob("*.py")):
        if path in STATION_EXEMPT:
            continue
        with open(path) as f:
            src = f.read()
        if not STATION_STATE.search(src):
            continue
        wired = "station.enter()" in src or "station.enter_all()" in src
        check(f"{path} chooses a station", wired,
              "" if wired else "reads station files (satellites.yaml, flowgraphs/, ...) "
              "but never calls station.enter() - in multi-station mode it would look "
              "in the project root and find nothing. Add `import station` and "
              "`station.enter()` as the first lines of main(), or list it in "
              "STATION_EXEMPT with the reason.")

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

        grc_path = sat.get("script", "").replace(".py", ".grc")
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
