#!/usr/bin/env python3
"""
Clear out old IQ recordings WITHOUT ever touching one that is still being written.

    python3 clean_iq.py                         # show what would go (changes nothing)
    python3 clean_iq.py --yes                   # move those files to the Trash
    python3 clean_iq.py --yes --archive ~/old_iq    # move them to another folder instead
    python3 clean_iq.py --yes --delete          # delete them for good
    python3 clean_iq.py --older-than 72          # only files older than 72 hours (default 24)

A file is left alone when ANY of these is true, so a recording in progress can never be caught:
  - a program (the flowgraph) still has it open
  - it was written to in the last --min-idle minutes (default 15)
  - it is newer than --older-than hours (default 24)

The folders searched are the IQ folders of this station's flowgraphs (read from their .grc files),
plus any you add with --dir. Only files ending in .iq directly in those folders are considered.
Why this exists: moving a file that is being recorded into the Trash lets the recorder keep writing
to it there, and the after-pass check then reports "IQ MISSING" although the data was recorded.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

import yaml

import passcheck

SKIP_OPEN = "still open (being recorded)"
SKIP_RECENT = "written to in the last {m} minutes"
SKIP_YOUNG = "newer than {h:g} hours"


def iq_dirs(cfg_path="satellites.yaml"):
    """The IQ folders named by this station's flowgraph .grc files (literal paths only)."""
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []
    out = []
    for s in cfg.get("satellites", []) or []:
        grc = passcheck.grc_text_for(s)
        sink = passcheck.sink_info(grc) if grc and os.path.exists(grc) else None
        d = sink and sink.get("basedir")
        if d and os.path.isdir(d):
            d = os.path.realpath(d)
            if d not in out:
                out.append(d)
    return out


def open_files():
    """Real paths of every file any process we may look at has open (Linux /proc)."""
    paths = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return paths
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    paths.add(os.path.realpath(os.readlink(f"/proc/{pid}/fd/{fd}")))
                except OSError:
                    pass
        except OSError:
            pass
    return paths


def candidates(dirs, older_hours=24.0, min_idle_min=15.0, now=None, opened=None):
    """([(path, size, age_hours)] that may go, [(path, size, reason)] left alone)."""
    now = time.time() if now is None else now
    opened = open_files() if opened is None else opened
    go, keep = [], []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if not n.endswith(".iq") or not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            age_s = now - st.st_mtime
            if os.path.realpath(p) in opened:
                keep.append((p, st.st_size, SKIP_OPEN))
            elif age_s < min_idle_min * 60:
                keep.append((p, st.st_size, SKIP_RECENT.format(m=min_idle_min)))
            elif age_s < older_hours * 3600:
                keep.append((p, st.st_size, SKIP_YOUNG.format(h=older_hours)))
            else:
                go.append((p, st.st_size, age_s / 3600))
    return go, keep


def to_trash(path):
    gio = shutil.which("gio")
    if not gio:
        raise RuntimeError("no 'gio' command to use the Trash - use --archive DIR or --delete")
    r = subprocess.run([gio, "trash", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "gio trash failed")


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--older-than", type=float, default=24.0, metavar="HOURS", help="only files older than this (default 24)")
    ap.add_argument("--min-idle", type=float, default=15.0, metavar="MINUTES", help="never touch a file written to within this many minutes (default 15)")
    ap.add_argument("--dir", action="append", default=[], help="also clean this folder (repeatable)")
    ap.add_argument("--yes", action="store_true", help="actually do it (without this, only shows what would happen)")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--archive", metavar="DIR", help="move files here instead of the Trash")
    how.add_argument("--delete", action="store_true", help="delete permanently instead of the Trash")
    args = ap.parse_args()

    dirs = iq_dirs() + [os.path.realpath(os.path.expanduser(d)) for d in args.dir]
    dirs = list(dict.fromkeys(dirs))
    if not dirs:
        sys.exit("no IQ folder found for this station (no flowgraph with a literal IQ folder) - give one with --dir")
    go, keep = candidates(dirs, args.older_than, args.min_idle)
    print("IQ folders: " + ", ".join(dirs))
    for p, size, why in keep:
        if why == SKIP_OPEN or why.startswith("written"):
            print(f"  KEEP  {os.path.basename(p)}  ({size / 1e6:.0f} MB) - {why}")
    total = sum(s for _, s, _ in go)
    if not go:
        print(f"Nothing to clean: no .iq file older than {args.older_than:g} hours that is idle.")
        return
    for p, size, age in go:
        print(f"  {'REMOVE' if args.yes else 'would remove'}  {os.path.basename(p)}  ({size / 1e6:.0f} MB, {age / 24:.1f} days old)")
    print(f"{len(go)} file(s), {total / 1e9:.2f} GB" + ("" if args.yes else " - nothing changed; add --yes to do it."))
    if not args.yes:
        return
    dest = os.path.expanduser(args.archive) if args.archive else None
    if dest:
        os.makedirs(dest, exist_ok=True)
    failed = 0
    for p, _, _ in go:
        try:
            if dest:
                shutil.move(p, os.path.join(dest, os.path.basename(p)))
            elif args.delete:
                os.remove(p)
            else:
                to_trash(p)
        except (OSError, RuntimeError, shutil.Error) as e:
            failed += 1
            print(f"  could not remove {os.path.basename(p)}: {e}")
    print(f"Done{'' if not failed else f' ({failed} could not be removed)'}.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
