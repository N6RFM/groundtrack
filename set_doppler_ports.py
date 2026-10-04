#!/usr/bin/env python3
"""
Point every flowgraph's Doppler poller at the right rigctld port - decided by which SDR the
flowgraph opens, so a station with two receivers on one antenna can't get them mixed up.

    python3 set_doppler_ports.py --radio BEAM --sdr airspy=4531 --sdr rtlsdr=4532 --dry-run
    python3 set_doppler_ports.py --radio BEAM --sdr airspy=4531 --sdr rtlsdr=4532 --station-port 4531

Each --sdr NAME=PORT is a rule: a flowgraph whose SDR source (its osmosdr/soapy args, e.g.
'driver=airspy,serial=...') contains NAME gets that port in its rig_freq_poller block. A
flowgraph that matches no rule, or several, is reported and left alone, as is one with no SDR
or no poller. --station-port also sets this station's own `rig_port:` in satellites.yaml (the
main Doppler channel - the one flowgraphs on it share).

Why by SDR: two receivers on one antenna must use different ports for run_passes.py to run them
together (see docs/stations.md), and "which port" is a property of the radio, not the satellite.

It edits one line per .grc - the poller's rig_port - as plain text, so nothing else in the file
changes, then recompiles each changed flowgraph that already has a compiled script (the port is
baked into it). It refuses while this station's run_passes.py is running. Every edit is checked
before anything is written, and if a write fails the ones already made are put back. --dry-run
shows the plan and changes nothing.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

import yaml

import lanes

POLLER_START = re.compile(r"^- name: rig_freq_poller\w*\s*$")
PORT_LINE = re.compile(r"^(\s+rig_port:\s*)(['\"]?)(\d+)(\2)(\s*)$")


def retarget_text(text, port):
    """The .grc text with the poller's rig_port set to `port` - that one line and nothing else.
    Returns (new text, number of lines changed)."""
    lines = text.split("\n")
    in_poller, changed = False, 0
    for i, line in enumerate(lines):
        if line.startswith("- name:"):
            in_poller = bool(POLLER_START.match(line))
            continue
        if in_poller:
            m = PORT_LINE.match(line)
            if m and int(m.group(3)) != port:
                lines[i] = f"{m.group(1)}{m.group(2)}{port}{m.group(4)}{m.group(5)}"
                changed += 1
    return "\n".join(lines), changed


def read_grc(grc):
    """(sdr arg strings, poller ports, flowgraph id) from a parsed .grc."""
    sdrs, ports = [], []
    for b in grc.get("blocks", []):
        p = b.get("parameters") or {}
        if str(b.get("id", "")).startswith(("osmosdr_source", "soapy")):
            sdrs.append(str(p.get("args") or p.get("dev") or ""))
        if b.get("id") == "epy_block" and "rig_freq_poller" in str(b.get("name", "")):
            ports.append(p.get("rig_port"))
    return sdrs, ports, (grc.get("options", {}).get("parameters", {}) or {}).get("id")


def running_pid():
    import station
    current = station.current()
    if current:
        return station.run_passes_pid(current)
    try:
        with open("run_passes.lock") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except PermissionError:
        return pid
    except (OSError, ValueError):
        return None


def repo_rel(path):
    """The path as git wants it (from the repository root), whatever folder we're standing in."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
        return os.path.relpath(os.path.abspath(path), top) if top else path
    except FileNotFoundError:
        return path


def git_state(path):
    """'clean' / 'edited' (tracked with uncommitted changes) / 'untracked' / 'no git'."""
    try:
        if subprocess.run(["git", "ls-files", "--error-unmatch", "--", path],
                          capture_output=True).returncode != 0:
            return "untracked"
        dirty = (subprocess.run(["git", "diff", "--quiet", "--", path]).returncode != 0 or
                 subprocess.run(["git", "diff", "--cached", "--quiet", "--", path]).returncode != 0)
        return "edited" if dirty else "clean"
    except FileNotFoundError:
        return "no git"


def parse_rules(specs):
    rules = {}
    for s in specs:
        name, sep, port = s.partition("=")
        if not sep or not name.strip() or not port.strip().isdigit():
            sys.exit(f"--sdr {s!r}: expected NAME=PORT, e.g. airspy=4531")
        rules[name.strip().lower()] = int(port)
    return rules


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdr", action="append", required=True, metavar="NAME=PORT",
                    help="a rule: flowgraphs whose SDR args contain NAME poll PORT (repeat for each SDR)")
    ap.add_argument("--station-port", type=int, metavar="PORT",
                    help="also set this station's own rig_port in satellites.yaml")
    ap.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    ap.add_argument("--no-compile", action="store_true", help="edit the .grc files but don't recompile")
    args = ap.parse_args()
    rules = parse_rules(args.sdr)

    pid = running_pid()
    if pid and not args.dry_run:
        sys.exit(f"run_passes.py is running for this station (PID {pid}) - stop it first: it feeds "
                 f"Doppler to the ports it started with, and recompiling under it would mix the two.")

    # ---- plan: read everything, decide, verify - touch nothing yet
    plan, notes = [], []
    id_owners = {}      # flowgraph id -> every .grc that carries it (grcc names its output after the id)
    for path in sorted(glob.glob("flowgraphs/*.grc")):
        try:
            with open(path, newline="") as f:
                text = f.read()
            sdrs, ports, fg_id = read_grc(yaml.safe_load(text))
            id_owners.setdefault(fg_id, []).append(path)
        except Exception as e:
            notes.append((path, f"skipped: can't read it as a flowgraph ({type(e).__name__})"))
            continue
        if not sdrs:
            notes.append((path, "skipped: no SDR source block")); continue
        if not ports:
            notes.append((path, "skipped: no rig_freq_poller block")); continue
        hit = sorted({k for k in rules if any(k in a.lower() for a in sdrs)})
        if not hit:
            notes.append((path, f"skipped: no rule for SDR '{sdrs[0]}'")); continue
        if len(hit) > 1:
            notes.append((path, f"skipped: its SDR matches several rules ({', '.join(hit)})")); continue
        try:
            current = sorted({int(str(p).strip().strip("'\"")) for p in ports})
        except ValueError:
            notes.append((path, f"skipped: its poller port isn't a plain number ({ports[0]!r})")); continue
        target = rules[hit[0]]
        if current == [target]:
            notes.append((path, f"{hit[0]:8} {target} (already right)")); continue
        new_text, n = retarget_text(text, target)
        if n == 0:
            notes.append((path, "skipped: couldn't find the poller's rig_port line to edit")); continue
        try:
            new_ports = [int(str(p).strip().strip("'\"")) for p in read_grc(yaml.safe_load(new_text))[1]]
        except Exception:
            new_ports = None
        changed_lines = sum(x != y for x, y in zip(text.split("\n"), new_text.split("\n")))
        if new_ports != [target] * len(ports) or changed_lines != n or len(text.split("\n")) != len(new_text.split("\n")):
            notes.append((path, "skipped: the edited file didn't verify, so it was not touched")); continue
        plan.append({"path": path, "text": text, "new": new_text, "sdr": hit[0], "old": current[0],
                     "target": target, "id": fg_id, "git": git_state(path)})

    station_edit = None
    if args.station_port is not None:
        try:
            with open("satellites.yaml") as f:
                ytext = f.read()
            m = re.search(r"^(rig_port:\s*)(\d+)(.*)$", ytext, re.M)
            if not m:
                notes.append(("satellites.yaml", "no top-level rig_port: line to edit"))
            elif int(m.group(2)) == args.station_port:
                notes.append(("satellites.yaml", f"rig_port {args.station_port} (already right)"))
            else:
                new_y = ytext[:m.start()] + f"{m.group(1)}{args.station_port}{m.group(3)}" + ytext[m.end():]
                if yaml.safe_load(new_y).get("rig_port") != args.station_port:
                    sys.exit("couldn't edit satellites.yaml safely - nothing changed")
                station_edit = {"text": ytext, "new": new_y, "old": int(m.group(2))}
        except OSError as e:
            notes.append(("satellites.yaml", f"can't read it ({e.strerror})"))

    print(f"Doppler ports by SDR: " + ", ".join(f"{k} -> {v}" for k, v in rules.items()) + "\n")
    for p in plan:
        print(f"  {p['path']:44} {p['sdr']:8} {p['old']} -> {p['target']}")
    for path, why in notes:
        print(f"  {path:44} {why}")
    if station_edit:
        print(f"  {'satellites.yaml':44} rig_port {station_edit['old']} -> {args.station_port}")
    if not plan and not station_edit:
        print("\nNothing to change.")
        return 0
    if args.dry_run:
        print("\n(--dry-run: nothing changed)")
        return 0

    # ---- write, putting everything back if any write fails
    done = []
    try:
        for p in plan:
            tmp = p["path"] + ".tmp"
            with open(tmp, "w", newline="") as f:
                f.write(p["new"])
            os.replace(tmp, p["path"])
            done.append((p["path"], p["text"]))
        if station_edit:
            with open("satellites.yaml", "w") as f:
                f.write(station_edit["new"])
            done.append(("satellites.yaml", station_edit["text"]))
    except OSError as e:
        for path, original in reversed(done):
            with open(path, "w", newline="") as f:
                f.write(original)
        sys.exit(f"write failed ({e}) - everything already changed has been put back")

    # ---- the port is baked into the compiled script, so rebuild the ones that exist
    failed = []
    if not args.no_compile:
        for p in plan:
            compiled = f"flowgraphs/{p['id']}.py"
            if not p["id"] or not os.path.exists(compiled):
                print(f"  {p['path']}: no compiled script yet - nothing to rebuild")
                continue
            # The compiled script is named after the flowgraph's ID, so two .grc files with the same id
            # (a template that is a copy of a real flowgraph, say) would overwrite each other's output.
            # Only the file actually named after the id owns the script.
            owners = id_owners.get(p["id"], [p["path"]])
            if len(owners) > 1:
                named = [o for o in owners if os.path.splitext(os.path.basename(o))[0] == p["id"]]
                if not named:
                    print(f"  {p['path']}: shares its flowgraph id '{p['id']}' with {', '.join(o for o in owners if o != p['path'])} and none is "
                          f"named '{p['id']}' - not rebuilt, since each would overwrite {compiled}; rebuild the one you use by hand")
                    continue
                if p["path"] not in named:
                    print(f"  {p['path']}: shares its flowgraph id '{p['id']}' with {named[0]}, so it would overwrite that one's "
                          f"compiled script - not rebuilt")
                    continue
            if shutil.which("grcc") is None:
                failed.append((p["path"], "grcc isn't installed here"))
                continue
            r = subprocess.run(["grcc", "-o", "flowgraphs", p["path"]], capture_output=True, text=True)
            if r.returncode != 0:
                failed.append((p["path"], (r.stderr or r.stdout).strip().splitlines()[-1:] or ["grcc failed"]))
            else:
                print(f"  rebuilt {compiled}")

    print(f"\nChanged {len(plan)} flowgraph(s)" + (f" and satellites.yaml's rig_port" if station_edit else "") + ".")
    if failed:
        print("\nCould not rebuild:")
        for path, why in failed:
            print(f"  {path}: {why}")
        print("The .grc files ARE changed. Rebuild them (./regen_all.sh, or grcc -o flowgraphs <file>) before running.")
    clean = [p["path"] for p in plan if p["git"] == "clean"]
    edited = [p["path"] for p in plan if p["git"] == "edited"]
    if clean or edited:
        print("\nFor git:")
        if clean:
            print("  tracked, nothing else changed - safe to commit as they are:\n    git add " + " ".join(repo_rel(c) for c in clean))
        if edited:
            print("  tracked, but you had UNCOMMITTED edits in these already - committing them includes those edits:\n    "
                  + "\n    ".join(repo_rel(c) for c in edited))
    print("\nNext: python3 preflight.py" + (f" --radio {station.current()}" if station.current() else "")
          + ", then restart run_passes.py for this station.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
