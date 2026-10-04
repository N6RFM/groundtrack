#!/usr/bin/env python3
"""
One script to check everything: which fleet folder you're actually
running from, whether any duplicate/stale copies exist elsewhere (e.g. in
Trash), which fleet-related processes are currently running and from
where, which of the fleet's ports are already occupied and by what, and
then (if all of that looks sane) the full satellites.yaml/.grc config
checks from preflight.py.

Usage:
    python3 doctor.py
    python3 doctor.py --live      # also passes --live through to preflight.py
"""

import glob
import os
import shutil
import yaml
import re
import socket
import subprocess
import sys
import time

# What doctor looks for is read from the configuration (see station.configured_ports() and
# configured_scripts()), not remembered here: a hardcoded list drifted twice - it listed
# satellites that weren't configured and knew nothing of a second station's rigctld port.
BASE_PROCESS_PATTERNS = ["relay.py", "tcp_bridge.py", "run_passes.py", "preflight.py", "rigctld", "rotctld"]
DEFAULT_PORTS = [(4532, "rigctld (Doppler)", set()), (4533, "rotctld (antenna)", set())]


def fleet_ports():
    import station
    # no readable configuration at all: still check the standard daemons' ports
    return station.configured_ports() or DEFAULT_PORTS


def fleet_process_patterns():
    import station
    return BASE_PROCESS_PATTERNS + sorted(station.configured_scripts())


def section(title):
    print(f"\n=== {title} ===")


def where_are_we():
    section("Where are we running from?")
    cwd = os.getcwd()
    real = os.path.realpath(cwd)
    print(f"Current directory: {cwd}")
    if real != cwd:
        print(f"Resolved (symlinks followed): {real}")
    if "Trash" in real:
        print("*** WARNING: this looks like it's inside a Trash folder. ***")
        print("    Your files may have been deleted/moved accidentally.")
        print("    Check your actual working folder (e.g. ~/Desktop/fleet)")
        print("    still exists and has your real satellites.yaml/schedule.yaml.")
    return real


def find_other_copies(current_real):
    """Copies of this project folder under the home directory. A project folder is one that
    holds the scripts (run_passes.py and station.py) AND some personal config: a
    satellites.yaml at its root (the classic layout) or in a folder one level down (one per
    station). A pristine clone with no config of its own isn't what this is hunting for; a
    second configured copy is - two of them is how you end up editing one and running the
    other. (It used to look only for run_passes.py beside satellites.yaml, which no folder
    satisfies once the configs live in station folders - so it went blind.)"""
    section("Looking for other copies of this project folder")
    home = os.path.expanduser("~")
    found = {}      # real path -> [folders holding a satellites.yaml, "" meaning the root itself]
    skip_dirs = {".cache", ".git", "node_modules"}
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        depth = root[len(home):].count(os.sep)
        if depth > 6:
            dirs[:] = []
            continue
        if "run_passes.py" in files and "station.py" in files:
            holders = ([""] if "satellites.yaml" in files else []) + sorted(
                d for d in dirs if not d.startswith(".") and os.path.exists(os.path.join(root, d, "satellites.yaml")))
            if holders:
                found[os.path.realpath(root)] = holders
    if not found:
        print("No configured project folders found under your home directory at all - odd, but not this script's problem.")
        return
    import datetime
    for path in sorted(found):
        holders = found[path]
        try:
            newest = max(os.path.getmtime(os.path.join(path, h, "satellites.yaml")) for h in holders)
            mtime_str = datetime.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime_str = "?"
        stations = [h for h in holders if h]
        what = (f"stations: {', '.join(stations)}; newest satellites.yaml modified {mtime_str}" if stations
                else f"satellites.yaml modified {mtime_str}")
        marker = "  <- you are here" if path == current_real else ""
        in_trash = "  *** IN TRASH ***" if "Trash" in path else ""
        print(f"  {path}  ({what}){marker}{in_trash}")
    if len(found) > 1:
        print(f"\n{len(found)} copies found - make sure you always cd into the same "
              f"one, and consider deleting/archiving the others to avoid confusion.")


def check_processes():
    section("Fleet-related processes currently running")
    patterns = fleet_process_patterns()
    try:
        out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Couldn't run `ps` - skipping process check.")
        return
    lines = out.splitlines()[1:]
    any_found = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        pid_str, _, args = line.partition(" ")
        tokens = args.split()
        if not tokens:
            continue
        exe_base = os.path.basename(tokens[0])

        matched = None
        if exe_base in ("rigctld", "rotctld") and exe_base in patterns:
            matched = exe_base
        elif exe_base.startswith("python"):
            # only match a .py pattern if it's actually the script being
            # run (a token whose basename equals the pattern), not merely
            # mentioned as an argument to some other command (cp, grep, etc)
            for tok in tokens[1:]:
                base = os.path.basename(tok)
                if base in patterns:
                    matched = base
                    break

        if matched:
            any_found = True
            cwd_path = None
            try:
                cwd_path = os.path.realpath(f"/proc/{pid_str}/cwd")
            except OSError:
                pass
            trash_flag = "  *** RUNNING FROM TRASH ***" if cwd_path and "Trash" in cwd_path else ""
            print(f"  PID {pid_str}: {args}{trash_flag}")
            if cwd_path:
                print(f"      cwd: {cwd_path}")
    if not any_found:
        print("  none found")


def port_owner(port):
    try:
        out = subprocess.run(["lsof", "-t", f"-i:{port}"], capture_output=True, text=True)
        pids = out.stdout.strip().splitlines()
        if not pids:
            return None
        pid = pids[0]
        name_out = subprocess.run(["ps", "-p", pid, "-o", "args="], capture_output=True, text=True)
        return f"PID {pid} ({name_out.stdout.strip()})"
    except FileNotFoundError:
        return "unknown (lsof not installed)"


def check_ports():
    section("Fleet ports")
    import station
    if station.multi_station():
        print("  (read from every station's configuration)")
    for port, label, rig_stations in fleet_ports():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            print(f"  {port:5d} ({label}): free")
        except OSError:
            s.close()
            owner = port_owner(port)
            print(f"  {port:5d} ({label}): IN USE - {owner or 'owner unknown'}")
            live = [st for st in sorted(rig_stations) if station.run_passes_pid(st)]
            if live:
                print(f"         expected: run_passes.py is running for station {', '.join(live)}, and it owns this rigctld")
            elif "rotctld (antenna)" in label and "rotctld" in (owner or ""):
                print(f"         expected: rotctld is running (you start it yourself for the beam)")
            else:
                print(f"         to free it: kill <PID above>, or pkill -f <process name>")


def run_preflight(extra_args):
    section("Config checks (preflight.py)")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    preflight_path = os.path.join(script_dir, "preflight.py")
    if not os.path.exists(preflight_path):
        print(f"preflight.py not found next to doctor.py at {script_dir} - skipping.")
        return
    subprocess.run([sys.executable, preflight_path] + extra_args)


def quick_status():
    """One compact block: TLE age, daemon status, next approved pass."""
    import yaml
    from datetime import datetime, timezone

    try:
        with open("satellites.yaml") as f:
            cfg = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"Can't read satellites.yaml: {e}")
        return

    # TLE age
    tle_path = cfg.get("tle_file", "")
    if os.path.exists(tle_path):
        age_hours = (time.time() - os.path.getmtime(tle_path)) / 3600
        tle_str = f"{age_hours:.1f}h old" + ("  ** REFRESH ME **" if age_hours > 48 else "")
    else:
        tle_str = "MISSING"
    print(f"TLE:      {tle_str}")

    # optional hand-maintained file for a satellite not yet in Celestrak/
    # SatNOGS - update_tle.py never touches this one, so its age isn't a
    # staleness signal the way tle_file's is; just confirm it's there
    custom_tle_path = cfg.get("custom_tle_file")
    if custom_tle_path:
        custom_str = "present" if os.path.exists(custom_tle_path) else "configured but not created yet"
        print(f"Custom TLE: {custom_str} ({custom_tle_path})")

    # daemons
    def port_status(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return "down"
        except OSError:
            s.close()
            return "up"

    rig_status = port_status(cfg.get("rig_port", 4532))
    print(f"rigctld:  {rig_status} (port {cfg.get('rig_port', 4532)})")
    if "rot_port" in cfg:
        rot_status = port_status(cfg["rot_port"])
        print(f"rotctld:  {rot_status} (port {cfg['rot_port']})")
    if cfg.get("satellites"):
        relay_sat = next((s for s in cfg["satellites"]
                           if s.get("enabled", True) and "consumer_port" in s), None)
        if relay_sat:
            relay_status = port_status(relay_sat["consumer_port"])
            print(f"relay.py: {relay_status}")
        else:
            print(f"relay.py: n/a (no enabled satellite uses the relay)")

    # next approved pass
    if os.path.exists("schedule.yaml"):
        with open("schedule.yaml") as f:
            sched = yaml.safe_load(f) or {}
        now = datetime.now(timezone.utc)
        upcoming = []
        for p in sched.get("passes", []):
            if not p.get("approved"):
                continue
            aos = datetime.fromisoformat(p["aos"].replace("Z", "+00:00"))
            if aos > now:
                upcoming.append((aos, p))
        if upcoming:
            upcoming.sort(key=lambda x: x[0])
            aos, p = upcoming[0]
            delta = aos - now
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            mins, _ = divmod(rem, 60)
            print(f"Next:     {p['name']} in {hours}h{mins:02d}m (AOS {p['aos']})")
        else:
            print("Next:     no approved future passes in schedule.yaml")
    else:
        print("Next:     no schedule.yaml - run plan_passes.py")


def check_stray_compiled_files(fix=False):
    section("Stray compiled flowgraphs (grcc writes to cwd, not the .grc's own folder)")
    grc_paths = sorted(glob.glob("flowgraphs/*.grc"))
    found_any = False

    for grc_path in grc_paths:
        slug = os.path.splitext(os.path.basename(grc_path))[0]

        # the main flowgraph .py - this one DOES belong in flowgraphs/,
        # since that's where satellites.yaml's script: path points
        stray_main = f"{slug}.py"
        if os.path.exists(stray_main):
            found_any = True
            correct_path = f"flowgraphs/{slug}.py"
            if os.path.exists(correct_path):
                # don't just assume the existing copy is fine - if the
                # stray is actually NEWER (a fresh compile that landed at
                # cwd right after editing the .grc), blindly deleting it
                # would silently keep the STALE copy in place instead,
                # exactly the kind of thing that produces a confusing
                # ".py is out of date with .grc" failure that persists
                # even after "cleaning up" the stray
                stray_is_newer = os.path.getmtime(stray_main) > os.path.getmtime(correct_path)
                if fix:
                    if stray_is_newer:
                        shutil.move(stray_main, correct_path)
                        print(f"  moved {stray_main} -> {correct_path} (the stray was "
                              f"actually newer - a fresh compile that landed at cwd; "
                              f"keeping it, not the older copy)")
                    else:
                        os.remove(stray_main)
                        print(f"  removed {stray_main} ({correct_path} is already "
                              f"current or newer)")
                else:
                    if stray_is_newer:
                        print(f"  {stray_main} - newer than {correct_path}! This is "
                              f"the fresh compile, not a redundant duplicate. Move it:")
                        print(f"    mv {stray_main} {correct_path}")
                    else:
                        print(f"  {stray_main} - stray duplicate, {correct_path} already "
                              f"exists correctly. Safe to remove:")
                        print(f"    rm {stray_main}")
            else:
                if fix:
                    shutil.move(stray_main, correct_path)
                    print(f"  moved {stray_main} -> {correct_path}")
                else:
                    print(f"  {stray_main} - landed here instead of {correct_path}. Move it:")
                    print(f"    mv {stray_main} {correct_path}")

        # Embedded Python blocks (epy_block): the compiled flowgraph IMPORTS a module per
        # block, named <flowgraph id>_<block name>.py ("import iss_sstv_rig_freq_poller_0 as
        # rig_freq_poller_0  # embedded python block"), found next to the main .py. So one of
        # these is REQUIRED beside the flowgraph - unlike a stray duplicate, it must never
        # just be deleted: that leaves a flowgraph that compiles fine and then dies at launch
        # with ModuleNotFoundError. One that landed in cwd is moved into flowgraphs/, and
        # dropped only when flowgraphs/ already has an equal-or-newer copy.
        try:
            with open(grc_path) as f:
                grc = yaml.safe_load(f)
            epy_names = [b["name"] for b in grc.get("blocks", []) if b.get("id") == "epy_block"]
            fg_id = str(grc["options"]["parameters"].get("id") or slug)
        except Exception:
            epy_names, fg_id = [], slug
        for name in epy_names:
            for prefix in dict.fromkeys([slug, fg_id]):     # grcc names it after the id; normally == slug
                stray_companion = f"{prefix}_{name}.py"
                if not os.path.exists(stray_companion):
                    continue
                found_any = True
                correct = f"flowgraphs/{stray_companion}"
                if os.path.exists(correct):
                    stray_is_newer = os.path.getmtime(stray_companion) > os.path.getmtime(correct)
                    if fix and stray_is_newer:
                        shutil.move(stray_companion, correct)
                        print(f"  moved {stray_companion} -> {correct} (the stray was newer - a fresh compile)")
                    elif fix:
                        os.remove(stray_companion)
                        print(f"  removed {stray_companion} ({correct} is already current or newer)")
                    elif stray_is_newer:
                        print(f"  {stray_companion} - newer than {correct}; it's the fresh compile. Move it:")
                        print(f"    mv {stray_companion} {correct}")
                    else:
                        print(f"  {stray_companion} - redundant, {correct} is already current or newer. Safe to remove:")
                        print(f"    rm {stray_companion}")
                else:
                    if fix:
                        shutil.move(stray_companion, correct)
                        print(f"  moved {stray_companion} -> {correct} (the compiled flowgraph imports this "
                              f"at launch - it has to sit beside the .py)")
                    else:
                        print(f"  {stray_companion} - the embedded block's module; the compiled flowgraph "
                              f"imports it at launch, so it belongs in flowgraphs/. Move it:")
                        print(f"    mv {stray_companion} {correct}")

    if not found_any:
        print("  none found")
    elif not fix:
        print(f"\n  This happens because 'grcc <path>' always writes its output - "
              f"the main flowgraph AND a companion file per embedded Python block - "
              f"to the current directory, ignoring the .grc's own folder. "
              f"'./regen_all.sh' avoids it entirely by passing -o flowgraphs "
              f"explicitly. Prefer that over calling grcc directly.\n"
              f"  Run 'python3 doctor.py --fix' to clean these up automatically.")
    else:
        print(f"\n  Done. Nothing else in this toolkit reads these files from the "
              f"repo root, so removing/moving them can't break anything that was "
              f"working - './regen_all.sh' would have overwritten flowgraphs/*.py "
              f"the same way on its next run regardless.")


def main():
    import station
    station.enter()
    if "--status" in sys.argv:
        quick_status()
        return

    fix = "--fix" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--fix"]

    extra_args = argv  # passed straight through to preflight.py
    real = where_are_we()
    import station
    # "you are here" is the project folder (where the scripts live) - not the station
    # folder this process has since changed into
    find_other_copies(os.path.realpath(station.SCRIPT_DIR))
    check_processes()
    check_ports()
    check_stray_compiled_files(fix=fix)
    run_preflight(extra_args)


if __name__ == "__main__":
    main()
