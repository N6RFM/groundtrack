#!/usr/bin/env python3
"""
Rename a station: its name in radios.yaml - what the GUI's station buttons, --radio
and GROUNDTRACK_STATION use - and, by default, its folder to match.

    python3 rename_station.py r2 BEAM
    python3 rename_station.py mini HELIX --label "Helix (fixed, no rotor)"
    python3 rename_station.py r2 BEAM --dir beam_station     # name the folder yourself
    python3 rename_station.py r2 BEAM --dry-run              # look first

The folder becomes the new name in lowercase (BEAM -> beam/) unless --dir says
otherwise. It's moved with git mv when it holds tracked files, so history follows
and the renames are staged for you to commit; otherwise a plain move. Everything
else lives inside the folder and goes with it: satellites.yaml, flowgraphs/,
schedule.yaml, pass_log.jsonl.

radios.yaml is edited as plain text, so your comments and layout survive (a copy
of the original is kept as radios.yaml.bak). The 'default:' line follows the
rename. Every precondition is checked up front and reported together: a station
that's running run_passes.py (stop it first), a name already taken (names are
case-insensitive), a folder that already exists. If any step fails, the ones
already done are undone.

Afterwards, anything that spells the old name has to change: a
GROUNDTRACK_STATION export in ~/.bashrc or a launcher, and any --radio you
typed from habit. Restart the GUI, and anything running for that station.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class RenameError(Exception):
    pass


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)


def tracked_under(rel):
    """True if git tracks files under this folder (so it must be moved with git mv)."""
    try:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return False
        return bool(git("ls-files", "--", rel).stdout.strip())
    except FileNotFoundError:
        return False


def write_radios(path, text):
    with open(path, "w") as f:
        f.write(text)


def load_with(path):
    """station.load_radios() against a given radios.yaml (restoring the real path after)."""
    import station
    real = station.RADIOS_PATH
    station.RADIOS_PATH = path
    try:
        return station.load_radios()
    finally:
        station.RADIOS_PATH = real


def quoted(label):
    return yaml.safe_dump(label, default_flow_style=True, width=10 ** 6).splitlines()[0].rstrip()


def edit_radios_text(text, old, new, new_dir, new_label):
    """The radios.yaml text with station `old` renamed to `new` (dir -> new_dir, and
    label -> new_label if given) and a default: that named it following along. A line
    edit, not a re-dump: comments and layout stay. Block style and one-line flow style
    ({dir: x, label: y}) are both handled."""
    lines = text.split("\n")
    key = re.compile(rf"^(\s+)(['\"]?){re.escape(old)}\2\s*:(.*)$")
    for i, line in enumerate(lines):
        m = key.match(line)
        if not m:
            continue
        indent, rest = m.group(1), m.group(3)
        if rest.strip() and not rest.strip().startswith("#"):          # flow style: one line
            rest = re.sub(r"(dir:\s*)[^,}]+", lambda mm: f"{mm.group(1)}{new_dir}", rest, count=1)
            if new_label is not None:
                rest = re.sub(r"(label:\s*)(\"[^\"]*\"|'[^']*'|[^,}]+)", lambda mm: f"{mm.group(1)}{quoted(new_label)}", rest, count=1)
            lines[i] = f"{indent}{new}:{rest}"
        else:                                                           # block style: edit the lines under it
            lines[i] = f"{indent}{new}:{rest}"
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > len(indent)):
                dm = re.match(r"^(\s+)dir:\s*(.*?)(\s+#.*)?$", lines[j])
                lm = re.match(r"^(\s+)label:\s*(.*?)(\s+#.*)?$", lines[j])
                if dm:
                    lines[j] = f"{dm.group(1)}dir: {new_dir}{dm.group(3) or ''}"
                elif lm and new_label is not None:
                    lines[j] = f"{lm.group(1)}label: {quoted(new_label)}{lm.group(3) or ''}"
                j += 1
        break
    else:
        raise RenameError(f"couldn't find a line for station {old!r} in radios.yaml to edit")
    out = "\n".join(lines)
    return re.sub(rf"^(default:\s*)(['\"]?){re.escape(old)}\2(\s*(#.*)?)$", rf"\g<1>{new}\g<3>", out, count=1, flags=re.M | re.I)


def build_plan(a):
    import station
    problems = []
    if not station.multi_station():
        raise RenameError("there's no radios.yaml - nothing to rename (see radios.example.yaml)")
    try:
        default, stations = station.load_radios()
    except station.StationError as e:
        raise RenameError(str(e))
    old = next((n for n in stations if n.lower() == a.old.lower()), None)
    if old is None:
        problems.append(f"no station called {a.old!r} (stations: {', '.join(stations)})")
    if not NAME_OK.match(a.new):
        problems.append(f"{a.new!r}: use letters, digits, - or _ (it's typed after --radio)")
    clash = next((n for n in stations if n.lower() == a.new.lower() and n != old), None)
    if clash:
        problems.append(f"{clash!r} is already a station (names are case-insensitive)")
    if old is None:
        return None, problems        # nothing further can be checked without a station to look at

    with open(station.RADIOS_PATH) as f:
        text = f.read()
    raw = yaml.safe_load(text)["stations"][old]
    raw_dir = str(raw["dir"])
    new_dir_value = a.dir or (a.new.lower() if not os.path.dirname(raw_dir) else os.path.join(os.path.dirname(raw_dir), a.new.lower()))
    old_abs = stations[old]["dir"]
    new_abs = os.path.normpath(os.path.expanduser(new_dir_value) if os.path.isabs(os.path.expanduser(new_dir_value)) else os.path.join(HERE, new_dir_value))
    moving = os.path.normpath(old_abs) != new_abs
    if moving and os.path.exists(new_abs):
        problems.append(f"{new_abs} already exists - refusing to merge into or overwrite it")
    if moving and not os.path.isdir(old_abs):
        problems.append(f"station {old!r}'s folder {old_abs} doesn't exist")
    pid = station.run_passes_pid(old)
    if pid:
        problems.append(f"run_passes.py is running for {old!r} (PID {pid}) - stop it first")
    if problems or not NAME_OK.match(a.new):
        return None, problems

    new_text = edit_radios_text(text, old, a.new, new_dir_value, a.label)
    try:                                         # prove the edited file means what we intend BEFORE touching anything
        fd, tmp = tempfile.mkstemp(suffix=".yaml"); os.close(fd)
        write_radios(tmp, new_text)
        new_default, new_stations = load_with(tmp)
    except Exception as e:
        raise RenameError(f"the edited radios.yaml wouldn't load ({e}) - nothing changed")
    finally:
        try: os.remove(tmp)
        except OSError: pass
    expect_keys = [a.new if n == old else n for n in stations]
    expect_default = a.new if default == old else default
    if list(new_stations) != expect_keys or new_default != expect_default or os.path.normpath(new_stations[a.new]["dir"]) != new_abs:
        raise RenameError("the edited radios.yaml doesn't say what was intended - nothing changed")
    return {"old": old, "new": a.new, "old_abs": old_abs, "new_abs": new_abs, "moving": moving,
            "text": text, "new_text": new_text, "was_default": default == old, "label": a.label}, []


def execute(plan):
    import station
    undo = []
    old_abs, new_abs = plan["old_abs"], plan["new_abs"]
    try:
        if plan["moving"]:
            rel_old = os.path.relpath(old_abs, HERE)
            rel_new = os.path.relpath(new_abs, HERE)
            if not rel_old.startswith("..") and tracked_under(rel_old):
                r = git("mv", rel_old, rel_new)
                if r.returncode != 0:
                    raise RenameError(f"git mv {rel_old} {rel_new} failed: {r.stderr.strip()}")
                undo.append(lambda: git("mv", rel_new, rel_old))
            else:
                shutil.move(old_abs, new_abs)
                undo.append(lambda: shutil.move(new_abs, old_abs))
        bak = station.RADIOS_PATH + ".bak"
        old_bak = open(bak).read() if os.path.exists(bak) else None     # an earlier rename's backup, if any
        shutil.copy2(station.RADIOS_PATH, bak)
        undo.append(lambda: os.remove(bak) if old_bak is None else write_radios(bak, old_bak))
        write_radios(station.RADIOS_PATH, plan["new_text"])
        undo.append(lambda: write_radios(station.RADIOS_PATH, plan["text"]))
        station.load_radios()                    # the file as written has to load
    except Exception as e:
        print(f"\nFAILED: {e}\nUndoing the steps already done ...", file=sys.stderr)
        for back in reversed(undo):
            try:
                back()
            except Exception as ue:
                print(f"  (couldn't undo one step: {ue})", file=sys.stderr)
        raise RenameError(str(e)) from e


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", help="the station's current name")
    ap.add_argument("new", help="its new name (letters, digits, - or _)")
    ap.add_argument("--dir", help="the new folder (default: the new name, lowercased)")
    ap.add_argument("--label", help="also change the label shown in the GUI")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args()
    try:
        plan, problems = build_plan(a)
    except RenameError as e:
        print(f"Can't rename: {e}", file=sys.stderr)
        return 1
    if problems:
        print("Can't rename yet:\n" + "\n".join(f"  - {x}" for x in problems), file=sys.stderr)
        return 1
    old, new = plan["old"], plan["new"]
    print(f"Rename station {old!r} to {new!r}:")
    print(f"  radios.yaml: {old} -> {new}" + ("  (and it stays the default)" if plan["was_default"] else "")
          + (f"; label -> {plan['label']!r}" if plan["label"] else ""))
    print(f"  folder: {os.path.relpath(plan['old_abs'], HERE)}/ -> {os.path.relpath(plan['new_abs'], HERE)}/"
          if plan["moving"] else "  folder: unchanged")
    if a.dry_run:
        print("\n(--dry-run: nothing changed)")
        return 0
    if not a.yes:
        try:
            ans = input("\nProceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("Not changed.")
            return 0
    try:
        execute(plan)
    except RenameError:
        return 1
    print(f"\nDone. The station is now {new!r}.")
    if os.environ.get("GROUNDTRACK_STATION", "").lower() == old.lower():
        print(f"NOTE: this shell has GROUNDTRACK_STATION={os.environ['GROUNDTRACK_STATION']} set - change it to {new}.")
    print(f"Also change any GROUNDTRACK_STATION export in ~/.bashrc or a launcher, and any --radio {old}.\n"
          f"Restart the GUI (and anything running for this station). Check it: python3 preflight.py --radio {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
