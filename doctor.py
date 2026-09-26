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

FLEET_PORTS = {
    4532: "rigctld (Doppler)",
    4533: "rotctld (antenna)",
    9101: "GEOSCAN-1 producer", 8101: "GEOSCAN-1 consumer",
    9102: "GEOSCAN-2 producer", 8102: "GEOSCAN-2 consumer",
    9103: "GEOSCAN-4 producer", 8103: "GEOSCAN-4 consumer",
    9104: "GEOSCAN-5 producer", 8104: "GEOSCAN-5 consumer",
}
FLEET_PROCESS_PATTERNS = [
    "relay.py", "run_passes.py", "preflight.py",
    "rigctld", "rotctld",
    "geoscan1.py", "geoscan2.py", "geoscan4.py", "geoscan5.py",
]


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
    section("Looking for other copies of this fleet folder")
    home = os.path.expanduser("~")
    found = []
    skip_dirs = {".cache", ".git", "node_modules"}
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        depth = root[len(home):].count(os.sep)
        if depth > 6:
            dirs[:] = []
            continue
        if "satellites.yaml" in files and "run_passes.py" in files:
            found.append(os.path.realpath(root))
    found = sorted(set(found))
    if not found:
        print("No fleet folders found under your home directory at all - odd, but not this script's problem.")
        return
    for path in found:
        marker = "  <- you are here" if path == current_real else ""
        in_trash = "  *** IN TRASH ***" if "Trash" in path else ""
        try:
            mtime = os.path.getmtime(os.path.join(path, "satellites.yaml"))
            import datetime
            mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime_str = "?"
        print(f"  {path}  (satellites.yaml modified {mtime_str}){marker}{in_trash}")
    if len(found) > 1:
        print(f"\n{len(found)} copies found - make sure you always cd into the same "
              f"one, and consider deleting/archiving the others to avoid confusion.")


def check_processes():
    section("Fleet-related processes currently running")
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
        if exe_base in ("rigctld", "rotctld") and exe_base in FLEET_PROCESS_PATTERNS:
            matched = exe_base
        elif exe_base.startswith("python"):
            # only match a .py pattern if it's actually the script being
            # run (a token whose basename equals the pattern), not merely
            # mentioned as an argument to some other command (cp, grep, etc)
            for tok in tokens[1:]:
                base = os.path.basename(tok)
                if base in FLEET_PROCESS_PATTERNS:
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
    for port, label in sorted(FLEET_PORTS.items()):
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

        # companion .py files for every embedded Python block (epy_block) -
        # grcc writes one per embedded block too, also to cwd. Unlike the
        # main flowgraph .py, nothing ever looks for these in flowgraphs/ -
        # they're pure disposable clutter, regenerated on every compile,
        # so the right fix is always to just delete them, never move them
        try:
            with open(grc_path) as f:
                grc = yaml.safe_load(f)
            epy_names = [b["name"] for b in grc.get("blocks", []) if b.get("id") == "epy_block"]
        except Exception:
            epy_names = []
        for name in epy_names:
            stray_companion = f"{slug}_{name}.py"
            if os.path.exists(stray_companion):
                found_any = True
                if fix:
                    os.remove(stray_companion)
                    print(f"  removed {stray_companion} (disposable, regenerated on every compile)")
                else:
                    print(f"  {stray_companion} - disposable companion file for an "
                          f"embedded Python block, regenerated on every compile:")
                    print(f"    rm {stray_companion}")

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
    if "--status" in sys.argv:
        quick_status()
        return

    fix = "--fix" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--fix"]

    extra_args = argv  # passed straight through to preflight.py
    real = where_are_we()
    find_other_copies(real)
    check_processes()
    check_ports()
    check_stray_compiled_files(fix=fix)
    run_preflight(extra_args)


if __name__ == "__main__":
    main()
