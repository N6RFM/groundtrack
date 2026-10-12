#!/usr/bin/env python3
"""
Bring THIS computer up to date in one command - for any groundtrack setup, one computer or several.

    python3 update_computer.py

What it does, each step checking first and skipping what is already done:

  1. works out which stations run on this computer (asked once, then remembered in computer.local.yaml;
     --ask-again to change it)
  2. pulls the latest from GitHub
  3. (only in a copy of this script that carries fixes) installs them, commits and pushes them
  4. finds this computer's own SDRs and records them in each station's sdr.local.yaml: it looks for the radios
     that are plugged in (SoapySDRUtil --find) and, where it cannot be sure which is which, asks
  5. runs preflight for each station here; if a flowgraph cannot record, clears GRC's stale block cache,
     recompiles and checks again
  6. restarts the GUI

Several computers sharing one GitHub repository each run this same command; what differs between them (which
stations, which radios) stays in files that are never committed. Nothing here deletes your files, edits your
recordings or touches the schedule.

    python3 update_computer.py --repo /path/to/groundtrack   # if it can't find the folder itself
    python3 update_computer.py --ask-again                   # choose this computer's stations again
    python3 update_computer.py --no-radios                   # leave the SDR settings alone
    python3 update_computer.py --no-gui                       # don't touch the GUI
"""

import argparse
import base64
import glob
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile

# Fixes carried inside a copy of this script (PAYLOAD at the bottom). Installed, committed and pushed only when
# the repo's payload_version.txt is older than PAYLOAD_VERSION. The copy that lives in the repository carries
# none (PAYLOAD_VERSION = 0), so this is a plain tool for everyone else.
PAYLOAD_VERSION = 0
VERSION_FILE = "payload_version.txt"
LOCAL_COMPUTER_FILE = "computer.local.yaml"

PY = sys.executable


def lbl(st):
    return st or "station"


def say(msg=""):
    print(msg, flush=True)


_step_no = [0]


def step(_ignored, title):
    _step_no[0] += 1
    say(f"\n=== {_step_no[0]}. {title} ===")


def run(cmd, cwd, env=None, check=False, capture=False, timeout=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        r = subprocess.run(cmd, cwd=cwd, env=e, stdin=subprocess.DEVNULL, capture_output=capture, text=True, timeout=timeout)
    except FileNotFoundError:
        r = subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")
    if check and r.returncode != 0:
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(cmd)}\n{(r.stdout or '')}{(r.stderr or '')}")
    return r


def ask(prompt, choices=None, default=None):
    while True:
        try:
            ans = input(prompt).strip().lower()
        except EOFError:
            ans = ""
        if not ans and default is not None:
            return default
        if choices is None or ans in choices:
            return ans
        say(f"  please answer one of: {', '.join(choices)}")


def find_repo(arg):
    candidates = [arg] if arg else [os.path.dirname(os.path.abspath(__file__)), os.getcwd(),
                                    "~/groundtrack", "~/Desktop/groundtrack"]
    for c in candidates:
        p = os.path.abspath(os.path.expanduser(c))
        if os.path.isdir(os.path.join(p, ".git")) and (os.path.exists(os.path.join(p, "satellites.yaml")) or
                                                      os.path.exists(os.path.join(p, "radios.yaml")) or
                                                      os.path.exists(os.path.join(p, "run_passes.py"))):
            return p
    raise SystemExit("I can't find the groundtrack folder. Run this from inside it, or use --repo /path/to/groundtrack.")


def all_stations(repo):
    """{name: folder} from radios.yaml, or {"": repo} for the classic single-folder layout."""
    path = os.path.join(repo, "radios.yaml")
    if not os.path.exists(path):
        return {"": repo}
    import yaml
    raw = yaml.safe_load(open(path)) or {}
    out = {}
    for name, spec in (raw.get("stations") or {}).items():
        d = os.path.expanduser(str((spec or {}).get("dir") or name))
        out[str(name)] = os.path.normpath(d if os.path.isabs(d) else os.path.join(repo, d))
    return out or {"": repo}


def local_stations(repo, ask_again):
    """The stations that run on THIS computer: remembered in computer.local.yaml, asked for the first time."""
    import yaml
    stations = all_stations(repo)
    if list(stations) == [""]:
        return stations
    path = os.path.join(repo, LOCAL_COMPUTER_FILE)
    saved = []
    try:
        saved = [str(s) for s in (yaml.safe_load(open(path)) or {}).get("stations", [])]
    except (OSError, AttributeError, yaml.YAMLError):
        pass
    saved = [s for s in saved if s in stations]
    if saved and not ask_again:
        return {s: stations[s] for s in saved}
    names = list(stations)
    say("This repository has several stations:")
    for i, n in enumerate(names, 1):
        say(f"  {i}) {n}")
    while True:
        ans = input("Which of them run on THIS computer? Numbers separated by commas, or Enter for all: ").strip()
        if not ans:
            chosen = names
            break
        try:
            chosen = [names[int(x) - 1] for x in re.split(r"[,\s]+", ans) if x]
            if chosen:
                break
        except (ValueError, IndexError):
            pass
        say("  please enter numbers from the list, e.g. 1,2")
    with open(path, "w") as f:
        f.write("# Which stations run on THIS computer. Not committed (see .gitignore). Change with: python3 update_computer.py --ask-again\n")
        yaml.safe_dump({"stations": chosen}, f, default_flow_style=False)
    ensure_ignored(repo, LOCAL_COMPUTER_FILE)
    say(f"Remembered in {LOCAL_COMPUTER_FILE}: {', '.join(chosen)}")
    return {s: stations[s] for s in chosen}


def ensure_ignored(repo, name):
    gi = os.path.join(repo, ".gitignore")
    try:
        text = open(gi).read()
    except OSError:
        text = ""
    if name not in text.split():
        with open(gi, "a") as f:
            f.write(("" if text.endswith("\n") or not text else "\n") + name + "\n")


def git_pull(repo):
    step(2, "Pull the latest from GitHub")
    r = run(["git", "pull", "--ff-only"], repo, capture=True)
    say((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        raise SystemExit("\ngit pull did not go through (see above). Nothing else was changed. "
                         "Send that message to whoever looks after this repository.")


def installed_version(repo):
    try:
        return int(open(os.path.join(repo, VERSION_FILE)).read().strip())
    except (OSError, ValueError):
        return 0


BEGIN_MARK = "# BEGIN " + "PAYLOAD"
END_MARK = "# END " + "PAYLOAD"


def strip_payload(text):
    """This script's own text without the fixes it carries - what gets committed to the repository."""
    a, b = text.index(BEGIN_MARK + "\n"), text.index(END_MARK)
    text = text[:a] + BEGIN_MARK + '\nPAYLOAD = ""\n' + text[b:]
    return re.sub(r"^PAYLOAD_VERSION = \d+", "PAYLOAD_VERSION = 0", text, flags=re.M)


def install_payload(repo):
    if not PAYLOAD.strip() or PAYLOAD_VERSION <= 0:
        return
    step(3, "Install the newest fixes")
    have = installed_version(repo)
    if have >= PAYLOAD_VERSION:
        say(f"Already up to date (version {have}) - nothing to do.")
        return
    z = zipfile.ZipFile(io.BytesIO(base64.b64decode("".join(PAYLOAD.split()))))
    names = list(z.namelist())
    # never overwrite something with uncommitted edits of your own
    dirty = []
    for n in names + ["update_computer.py"]:
        if os.path.exists(os.path.join(repo, n)):
            if run(["git", "ls-files", "--error-unmatch", n], repo, capture=True).returncode == 0 and \
               run(["git", "diff", "--quiet", "--", n], repo).returncode != 0:
                dirty.append(n)
    if dirty:
        raise SystemExit("These files have changes of your own that are not committed, so I will not overwrite them: "
                         + ", ".join(dirty) + "\nCommit or discard them (git status), then run this again.")
    for n in names:
        dest = os.path.join(repo, n)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(z.read(n))
    with open(os.path.join(repo, "update_computer.py"), "w") as f:
        f.write(strip_payload(open(os.path.abspath(__file__)).read()))
    names.append("update_computer.py")
    say(f"Installed {len(names)} files into {repo} (version {have} -> {PAYLOAD_VERSION}).")
    for n in names:
        run(["git", "add", n], repo)
    staged = run(["git", "diff", "--cached", "--name-only"], repo, capture=True).stdout.split()
    extra = [n for n in staged if n not in names]
    if extra:
        raise SystemExit("Other files were already staged, so I will not commit: " + ", ".join(extra) +
                         "\nRun `git reset` in the repo and start this script again.")
    if not staged:
        say("Nothing new to commit.")
        return
    say("Files to commit: " + ", ".join(staged))
    if ask("Commit and push these to GitHub now? [Y/n]: ", ("y", "n"), "y") != "y":
        say("Left uncommitted. Run this script again when ready.")
        return
    run(["git", "commit", "-m", f"update_computer.py: fixes, version {PAYLOAD_VERSION}"], repo, check=True)
    r = run(["git", "push"], repo, capture=True)
    say((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        say("\nThe push was refused (probably newer changes on GitHub). Run `git pull` in the repo, then this script again.")


def detect_radios():
    """{driver: [serial or None, ...]} for the SDRs plugged in, from SoapySDRUtil --find; None if that tool is missing."""
    r = run(["SoapySDRUtil", "--find"], None, capture=True, timeout=60)
    if r.returncode == 127:
        return None
    found, cur = {}, {}
    for line in (r.stdout + r.stderr).splitlines() + ["Found device"]:
        m = re.match(r"\s*(\w+)\s*=\s*(.*)$", line)
        if m:
            cur[m.group(1)] = m.group(2).strip()
        elif line.startswith("Found device") or not line.strip():
            if cur.get("driver"):
                found.setdefault(cur["driver"], []).append(cur.get("serial"))
            cur = {}
    return found


def with_serial(committed, serial):
    """The committed device string with this computer's serial number in place of (or added to) the one in it."""
    out = re.sub(r"serial=[^,\s]+", f"serial={serial}", committed) if "serial=" in committed else f"{committed},serial={serial}"
    return re.sub(r"soapy=\d+", "soapy=0", out)    # that index belongs to the other computer's setup; 0 is the usual one


def station_drivers(folder, sdr_local):
    """{driver: committed device string} for the SDR types this station's flowgraphs open (real flowgraphs first)."""
    out = {}
    grcs = glob.glob(os.path.join(folder, "flowgraphs", "*.grc"))
    for g in sorted(grcs, key=lambda p: (os.path.basename(p).startswith("_"), os.path.basename(p))):
        try:
            dev = sdr_local.grc_source_args(open(g, newline="").read())
        except OSError:
            continue
        drv = sdr_local.driver_of(dev)
        if drv and drv not in out:
            out[drv] = (dev, os.path.relpath(g, folder))
    return out


def set_sdr(repo, stations):
    step(4, "Record this computer's own SDR device strings")
    sys.path.insert(0, repo)
    import sdr_local
    detected = detect_radios()
    if detected is None:
        say("(SoapySDRUtil isn't installed, so I can't see which radios are plugged in - I'll ask instead.)")
    elif not detected:
        say("(No SDR is plugged in right now.)")
    for st, folder in stations.items():
        label = st or "station"
        if not os.path.isdir(folder):
            say(f"[{label}] no such folder here - skipped")
            continue
        here = os.getcwd()
        os.chdir(folder)                      # sdr.local.yaml lives in the station folder
        try:
            current = sdr_local.load_local()
            drivers = station_drivers(folder, sdr_local)
            changed = False
            for drv, (committed, rel) in drivers.items():
                if drv in current:
                    continue
                serials = [s for s in (detected or {}).get(drv, []) if s]
                chosen = None
                if committed and sdr_local.serial_of(committed) in serials:
                    chosen = committed                       # the committed string already names a radio plugged in here
                elif len(serials) == 1:
                    chosen = with_serial(committed, serials[0])
                elif len(serials) > 1:
                    say(f"[{label}] {len(serials)} {drv} radios are plugged in:")
                    for i, s in enumerate(serials, 1):
                        say(f"    {i}) serial {s}")
                    pick = ask(f"    Which one does station '{label}' use? [1-{len(serials)}, Enter to skip]: ",
                               tuple(str(i) for i in range(1, len(serials) + 1)), "")
                    if pick:
                        chosen = with_serial(committed, serials[int(pick) - 1])
                elif detected is None or not detected.get(drv):
                    say(f"[{label}] no {drv} radio found. {rel} says: {committed}")
                    if ask("    Is that this computer's radio anyway? [y/N]: ", ("y", "n"), "n") == "y":
                        chosen = committed
                if chosen:
                    current[drv] = chosen
                    changed = True
                    say(f"[{label}] {drv} -> {chosen}")
                else:
                    say(f"[{label}] {drv}: not set (preflight will warn if a flowgraph needs it)")
            if changed:
                sdr_local.save_local(current)
                ensure_ignored(repo, "sdr.local.yaml")
            elif not drivers:
                say(f"[{label}] no SDR flowgraph found - nothing to record")
            else:
                say(f"[{label}] already recorded: " + ", ".join(sorted(current)))
            for g in sorted(glob.glob(os.path.join(folder, "flowgraphs", "*.grc"))):
                if os.path.basename(g).startswith("_"):
                    continue
                dev = sdr_local.grc_source_args(open(g, newline="").read())
                want = current.get(sdr_local.driver_of(dev) or "")
                if dev and want and not sdr_local.same_device(want, dev):
                    say(f"[{label}] note: {os.path.basename(g)} opens a different {sdr_local.driver_of(dev)} "
                        f"({sdr_local.serial_of(dev)}) than this computer's ({sdr_local.serial_of(want)}). Fine if that "
                        f"satellite isn't used here.")
        finally:
            os.chdir(here)


def regenerate(repo, st, folder):
    cache = os.path.expanduser("~/.cache/grc_gnuradio")
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)
        say(f"[{lbl(st)}] cleared GRC's block cache")
    for g in sorted(glob.glob(os.path.join(folder, "flowgraphs", "*.grc"))):
        if os.path.basename(g).startswith("_"):
            continue
        r = run(["grcc", "-o", "flowgraphs", os.path.relpath(g, folder)], folder, capture=True, timeout=300)
        say(f"[{lbl(st)}] recompiled {os.path.basename(g)}" if r.returncode == 0 else f"[{lbl(st)}] grcc FAILED for {os.path.basename(g)}")


def preflight(repo, stations):
    step(5, "Check every station")
    all_ok = True
    for st, folder in stations.items():
        if not os.path.isdir(folder):
            continue
        for attempt in (1, 2):
            r = run([PY, os.path.join(repo, "preflight.py")], folder, env=({"GROUNDTRACK_STATION": st} if st else None), capture=True, timeout=600)
            out = r.stdout + r.stderr
            fails = [l for l in out.splitlines() if l.startswith("[FAIL]")]
            summary = next((l for l in out.splitlines() if re.search(r"\d+ passed", l)), "")
            if fails and attempt == 1 and any("Record On Start" in l or "passes Record" in l for l in fails):
                say(f"[{lbl(st)}] a flowgraph can't record - clearing GRC's stale cache and recompiling, then checking again")
                regenerate(repo, st, folder)
                continue
            break
        say(f"[{lbl(st)}] {summary.strip() or 'preflight did not finish'}")
        for l in fails:
            say("      " + l)
        all_ok = all_ok and not fails
    return all_ok


def restart_gui(repo):
    step(6, "Restart the GUI")
    run(["pkill", "-f", "groundtrack_gui.py"], repo)
    if os.environ.get("DISPLAY") and ask("Start the GUI again now? [Y/n]: ", ("y", "n"), "y") == "y":
        subprocess.Popen([PY, "groundtrack_gui.py"], cwd=repo, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        say("GUI started.")
    else:
        say("Old GUI stopped. Start it with: python3 groundtrack_gui.py")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="the groundtrack folder (default: found automatically)")
    ap.add_argument("--ask-again", action="store_true", help="choose which stations run on this computer again")
    ap.add_argument("--no-radios", action="store_true", help="don't look at the SDR settings")
    ap.add_argument("--no-gui", action="store_true", help="don't touch the GUI")
    args = ap.parse_args()

    repo = find_repo(args.repo)
    step(1, "This computer")
    say(f"Repository: {repo}")
    git_pull(repo)
    install_payload(repo)
    stations = local_stations(repo, args.ask_again)
    say("Stations here: " + (", ".join(stations) if list(stations) != [""] else "(single station)"))
    if not args.no_radios:
        set_sdr(repo, stations)
    ok = preflight(repo, stations)
    if not args.no_gui:
        restart_gui(repo)
    say("\nAll done." if ok else "\nDone, but a check above failed - the lines marked [FAIL] say what.")


# BEGIN PAYLOAD
PAYLOAD = ""
# END PAYLOAD


if __name__ == "__main__":
    main()
