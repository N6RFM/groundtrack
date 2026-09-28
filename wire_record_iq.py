#!/usr/bin/env python3
"""
Wires a satellite's .grc for the per-run IQ recording toggle, in one step.

The toggle needs two things in the flowgraph, both of which used to be done
by hand in GRC:
  1. a Parameter block named record_iq (type int) - this is what makes the
     compiled script accept a --record-iq command-line option at all, and
  2. the Advanced File Sink's Record On Start set to bool(record_iq)
     instead of a fixed True/False.

Behavior is unchanged until something actually passes --record-iq: the new
parameter's default is taken from what Record On Start was before (True ->
1, False -> 0), so a flowgraph that always recorded still always records.

This is one of the few tools that WRITES to a .grc (see "Adding a satellite"
in the docs for why that's normally off the table), so it's deliberately
careful:
  - edits the file as raw text, changing one line and inserting one block -
    never a load/dump round-trip, which would reformat the whole file
  - shows a diff first and asks before writing (--yes skips the question,
    --dry-run only shows the diff)
  - keeps a .bak of the original, and re-checks the result; if the result
    doesn't pass, the original is restored
  - refuses, changing nothing, for anything that needs a human decision
    (Record On Start is some other expression, a block named record_iq that
    isn't a Parameter block, a disabled record_iq, unexpected file layout)
  - does nothing if the flowgraph is already wired

It does not recompile, and it does not touch satellites.yaml. After it runs:
    ./regen_all.sh
    python3 edit_satellite.py NAME --record-iq-toggle

Usage:
    python3 wire_record_iq.py BY70-4 JAMX-01
    python3 wire_record_iq.py BY70-4 --dry-run
"""

import argparse
import difflib
import os
import shutil
import sys
import tempfile
import time

import yaml

from edit_satellite import grc_wiring_problem

CONFIG_PATH = "satellites.yaml"
SINK_NAME = "filerepeater_AdvFileSink_0"
PARAM_NAME = "record_iq"


class Refusal(Exception):
    """A reason this file can't be wired safely without a human deciding
    something - never a crash, always reported and the file left alone."""


def param_block_lines(default, y):
    """The Parameter block, key for key the shape GRC itself writes (compare
    any existing Parameter block, e.g. freq, in the same file)."""
    return [
        f"- name: {PARAM_NAME}",
        "  id: parameter",
        "  parameters:",
        "    alias: ''",
        "    comment: 'record IQ on this run? 1 = yes, 0 = no'",
        "    hide: none",
        f"    label: {PARAM_NAME}",
        "    short_id: ''",
        "    type: intx",
        f"    value: '{default}'",
        "  states:",
        "    bus_sink: false",
        "    bus_source: false",
        "    bus_structure: null",
        f"    coordinate: [72, {y:.1f}]",
        "    rotation: 0",
        "    state: enabled",
    ]


def plan_edits(text):
    """Returns (new_text, actions), or (None, []) if already wired. Raises
    Refusal for anything that isn't a safe, mechanical change."""
    if "\r" in text:
        raise Refusal("this file has Windows line endings; expected the plain "
                      "line endings GRC writes")
    grc = yaml.safe_load(text)
    blocks = {b["name"]: b for b in grc.get("blocks", [])}

    sink = blocks.get(SINK_NAME)
    if sink is None:
        raise Refusal(f"no {SINK_NAME} block - there's no recording block for "
                      f"the toggle to control")
    ros = str(sink["parameters"].get("recordOnStart", ""))

    param = blocks.get(PARAM_NAME)
    if param is not None:
        if param.get("id") != "parameter":
            raise Refusal(f"a block named {PARAM_NAME} already exists but it's "
                          f"a {param.get('id')!r}, not a Parameter block - "
                          f"rename or remove it in GRC first")
        state = param.get("states", {}).get("state", "enabled")
        if state != "enabled":
            raise Refusal(f"the {PARAM_NAME} Parameter block exists but is "
                          f"{state} - enable it in GRC (disabled blocks are left "
                          f"out of the compiled script)")

    need_field = PARAM_NAME not in ros
    need_param = param is None
    if not need_field and not need_param:
        return None, []

    lines = text.split("\n")
    actions = []
    default = "1"

    if need_field:
        norm = ros.strip().strip("'\"").lower()
        if norm == "true":
            default = "1"
        elif norm == "false":
            default = "0"
        else:
            raise Refusal(f"Record On Start is currently {ros!r}; only a plain "
                          f"True or False can be converted safely - set it to "
                          f"bool({PARAM_NAME}) by hand")
        try:
            start = lines.index(f"- name: {SINK_NAME}")
        except ValueError:
            raise Refusal("couldn't find the sink block in the layout GRC "
                          "normally writes")
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i] and not lines[i].startswith(" "):
                end = i  # next top-level item or key
                break
        hits = [i for i in range(start, end)
                if lines[i].startswith("    recordOnStart:")]
        if len(hits) != 1:
            raise Refusal(f"expected exactly one recordOnStart line in the sink "
                          f"block, found {len(hits)}")
        lines[hits[0]] = f"    recordOnStart: bool({PARAM_NAME})"
        actions.append(f"set the Advanced File Sink's Record On Start to "
                       f"bool({PARAM_NAME}) (was {ros!r})")

    if need_param:
        try:
            conn = lines.index("connections:")
        except ValueError:
            raise Refusal("couldn't find the connections: section to insert "
                          "the new block before")
        insert_at = conn - 1 if conn > 0 and lines[conn - 1] == "" else conn
        ys = []
        for b in grc.get("blocks", []):
            c = (b.get("states") or {}).get("coordinate")
            if c and len(c) == 2 and isinstance(c[1], (int, float)):
                ys.append(float(c[1]))
        y = (max(ys) if ys else 0.0) + 200.0  # below everything: never overlaps
        lines[insert_at:insert_at] = param_block_lines(default, y)
        actions.append(f"add a {PARAM_NAME} Parameter block (int, default "
                       f"{default} - same behavior as before until --record-iq "
                       f"says otherwise), placed below the other blocks on the "
                       f"canvas")

    return "\n".join(lines), actions


def verify_text(new_text):
    """Runs the same wiring check edit_satellite.py and preflight.py use, on
    the proposed text, without touching the real file."""
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        return f"result isn't valid YAML: {e}"
    fd, tmp = tempfile.mkstemp(suffix=".grc")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        return grc_wiring_problem(tmp)
    finally:
        os.unlink(tmp)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="+", help="satellite name(s), as in satellites.yaml")
    ap.add_argument("--dry-run", action="store_true",
                     help="show what would change, write nothing")
    ap.add_argument("--yes", "-y", action="store_true",
                     help="apply without asking for confirmation")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    sats = {s["name"]: s for s in cfg.get("satellites", [])}
    unknown = [n for n in args.names if n not in sats]
    if unknown:
        sys.exit(f"Not in {CONFIG_PATH}: {', '.join(unknown)}. "
                 f"Configured: {', '.join(sats)}")

    wired, problems = [], 0
    for name in args.names:
        script = sats[name].get("script", "")
        grc = script.replace(".py", ".grc") if script else ""
        print(f"\n{name}: {grc or '(no script: set)'}")
        if not grc:
            print("  SKIPPED: no script: set in satellites.yaml")
            problems += 1
            continue
        try:
            # newline="" = no translation: Python's default would silently
            # turn Windows line endings into plain ones on read (so the
            # Refusal check below could never fire) and the write would then
            # change every line ending in the file without the diff showing it
            with open(grc, newline="") as f:
                text = f.read()
        except FileNotFoundError:
            print("  SKIPPED: that file doesn't exist - build the flowgraph first")
            problems += 1
            continue
        try:
            new_text, actions = plan_edits(text)
        except Refusal as r:
            print(f"  REFUSED: {r}")
            problems += 1
            continue
        if new_text is None:
            print("  already wired - nothing to do")
            continue

        bad = verify_text(new_text)
        if bad:
            print(f"  REFUSED: the change I'd make doesn't pass its own check "
                  f"({bad}); nothing written")
            problems += 1
            continue

        for a in actions:
            print(f"  will: {a}")
        diff = difflib.unified_diff(text.split("\n"), new_text.split("\n"),
                                    fromfile=grc, tofile=f"{grc} (after)",
                                    lineterm="", n=2)
        print("\n".join("    " + line for line in diff))

        if args.dry_run:
            print("  (--dry-run: nothing written)")
            continue
        if not args.yes:
            try:
                ans = input(f"  Apply this change to {grc}? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans != "y":
                print("  not applied.")
                continue

        bak = grc + ".bak"
        if os.path.exists(bak):
            bak = f"{grc}.bak.{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(grc, bak)
        with open(grc, "w", newline="") as f:
            f.write(new_text)
        bad = grc_wiring_problem(grc)
        if bad:
            shutil.copy2(bak, grc)
            print(f"  ERROR: the written file failed verification ({bad}); "
                  f"restored the original from {bak}")
            problems += 1
            continue
        print(f"  wrote {grc} (backup: {bak})")
        wired.append(name)

    if wired:
        print("\nIf any of these flowgraphs is open in GRC, close it WITHOUT saving "
              "before going on, and reopen it afterwards: GRC doesn't notice the "
              "file changed on disk, and saving (or Generate/Run) from the old "
              "window would silently undo this.")
        print("\nNext - recompile, then declare the capability (nothing changes "
              "for these satellites until the flag is passed; the default "
              "preserves what Record On Start was before):")
        print("  ./regen_all.sh")
        for n in wired:
            print(f"  python3 edit_satellite.py {n} --record-iq-toggle")
        print("  python3 preflight.py")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
