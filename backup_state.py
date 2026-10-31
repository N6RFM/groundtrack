#!/usr/bin/env python3
"""
Back up everything about THIS computer's setup that is not in GitHub, and restore it after a disk failure.

    python3 backup_state.py                         # make a backup now (into ~/groundtrack-backups)
    python3 backup_state.py --list                  # backups on this computer, newest first
    python3 backup_state.py --list FILE.zip         # what is inside one (changes nothing)
    python3 backup_state.py --restore               # show what restoring the newest backup would do (changes nothing)
    python3 backup_state.py --restore --yes         # ...and do it (replaced files are kept as .bak-<time>)
    python3 backup_state.py --restore FILE.zip --yes
    python3 backup_state.py --scp user@host:dir/    # also copy each new backup to another machine (your own server, a NAS)
    python3 backup_state.py --scp user@host:dir/ --remember   # ...and remember that destination (backup.local.yaml)
    python3 backup_state.py --schedule              # back up automatically every Sunday (systemd user timer)
    python3 backup_state.py --schedule daily        # ...or every day (also: monthly)
    python3 backup_state.py --unschedule
    python3 backup_state.py --check                 # when was the last backup, and is the schedule on?

What is saved (everything GitHub does not have):
  - radios.yaml, computer.local.yaml, sdr.local.yaml (root and per station)
  - each station's satellites.yaml, schedule.yaml, pass_ack.json, pass_log.jsonl and every flowgraph .grc
  - tle/ (custom TLE files live there too)
What is NOT saved: the repository's own code (GitHub has it), compiled flowgraphs (rebuilt from the .grc),
IQ recordings and KISS files (large - they are your data, back those up your own way).

PRIVACY: satellites.yaml holds your ground-station coordinates, so a backup does too. The file is made readable
by you only, "*.zip" is already ignored by git, and it must never be uploaded to GitHub. Copy it only to machines you own.

A backup identical to the previous one is skipped, so a daily schedule does not pile up files. The newest
--keep (default 5) are kept and older ones removed - only files this script made, nothing else in the folder.
Restoring on a different computer leaves sdr.local.yaml and computer.local.yaml alone (they describe the OTHER
computer's radios and stations) unless you add --include-computer-files.
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from datetime import datetime

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
FORMAT = 1
DEFAULT_DIR = os.path.join("~", "groundtrack-backups")
LOCAL_CONFIG = "backup.local.yaml"
COMPUTER_FILES = ("sdr.local.yaml", "computer.local.yaml")
PREFIX = "groundtrack-backup-"
MANIFEST = "backup-manifest.json"
KEY = os.path.join("~", ".ssh", "id_ed25519_groundtrack")
UNIT = "groundtrack-backup"
STATION_FILES = ("satellites.yaml", "schedule.yaml", "pass_ack.json", "pass_log.jsonl", "sdr.local.yaml", ".satnogs_names.json")
ROOT_FILES = ("radios.yaml", "computer.local.yaml", "sdr.local.yaml", "satellites.yaml", "schedule.yaml",
              "pass_ack.json", "pass_log.jsonl")


def say(msg=""):
    print(msg, flush=True)


def station_dirs(root):
    """Absolute folders of every station in radios.yaml that exists (empty list in single-station mode)."""
    path = os.path.join(root, "radios.yaml")
    try:
        raw = yaml.safe_load(open(path)) or {}
    except (OSError, yaml.YAMLError):
        return []
    out = []
    for spec in (raw.get("stations") or {}).values():
        d = os.path.expanduser(str((spec or {}).get("dir") or ""))
        if not d:
            continue
        d = os.path.normpath(d if os.path.isabs(d) else os.path.join(root, d))
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def collect_files(root):
    """-> sorted list of paths relative to root (forward slashes) that belong in a backup."""
    found = set()

    def add(rel):
        if os.path.isfile(os.path.join(root, rel)):
            found.add(rel.replace(os.sep, "/"))

    for f in ROOT_FILES:
        add(f)
    for d in station_dirs(root):
        rel = os.path.relpath(d, root)
        if rel.startswith(".."):
            continue  # a station folder outside the project: not ours to carry around
        for f in STATION_FILES:
            add(os.path.join(rel, f))
        gdir = os.path.join(d, "flowgraphs")
        if os.path.isdir(gdir):
            for n in os.listdir(gdir):
                if n.endswith(".grc"):
                    add(os.path.join(rel, "flowgraphs", n))
    if os.path.isdir(os.path.join(root, "flowgraphs")):
        for n in os.listdir(os.path.join(root, "flowgraphs")):
            if n.endswith(".grc"):
                add(os.path.join("flowgraphs", n))
    tle = os.path.join(root, "tle")
    if os.path.isdir(tle):
        for n in os.listdir(tle):
            if n != ".gitkeep":
                add(os.path.join("tle", n))
    return sorted(found)


def digest(root, files):
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode() + b"\0")
        with open(os.path.join(root, rel), "rb") as f:
            h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def load_local(root):
    try:
        return yaml.safe_load(open(os.path.join(root, LOCAL_CONFIG))) or {}
    except (OSError, yaml.YAMLError):
        return {}


def backup_dir(root, arg=None):
    return os.path.expanduser(arg or load_local(root).get("dir") or DEFAULT_DIR)


def archives(folder):
    try:
        names = [n for n in os.listdir(folder) if n.startswith(PREFIX) and n.endswith(".zip")]
    except OSError:
        return []
    return sorted((os.path.join(folder, n) for n in names), reverse=True)  # names sort by time


def read_manifest(path):
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read(MANIFEST))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def make_backup(root, folder, keep, force=False, say=say):
    """-> (path or None, reason). None means nothing new to save (identical to the newest backup)."""
    files = collect_files(root)
    if not files:
        raise SystemExit("nothing to back up here - run this from the groundtrack folder")
    dg = digest(root, files)
    old = archives(folder)
    if old and not force:
        m = read_manifest(old[0])
        if m and m.get("digest") == dg:
            return None, f"unchanged since {os.path.basename(old[0])}"
    os.makedirs(folder, mode=0o700, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, f"{PREFIX}{stamp}.zip")
    manifest = {"format": FORMAT, "host": socket.gethostname(), "created": datetime.now().isoformat(timespec="seconds"),
                "digest": dg, "files": files}
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=1))
        for rel in files:
            z.write(os.path.join(root, rel), rel)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    # verify what we just wrote before trusting it (and before deleting any older one)
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
    if bad:
        os.remove(path)
        raise SystemExit(f"the new backup failed its own check ({bad}); nothing was kept")
    removed = 0
    for old_path in archives(folder)[max(keep, 1):]:
        try:
            os.remove(old_path)
            removed += 1
        except OSError:
            pass
    say(f"saved {len(files)} files -> {path}" + (f"  (removed {removed} old backup(s))" if removed else ""))
    return path, "saved"


def record_last(root, ok, note):
    try:
        with open(os.path.join(root, ".last_backup.json"), "w") as f:
            json.dump({"time": time.time(), "ok": ok, "note": note}, f)
    except OSError:
        pass


def copy_out(path, dest, say=say):
    if not shutil.which("scp"):
        say("scp not found - copy the file yourself")
        return False
    cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    key = os.path.expanduser(KEY)
    if os.path.exists(key):
        cmd += ["-i", key]
    r = subprocess.run(cmd + [path, dest], capture_output=True, text=True)
    if r.returncode != 0:
        say(f"copy to {dest} FAILED: {(r.stderr or r.stdout).strip().splitlines()[-1:] or ['unknown error']}")
        return False
    say(f"copied to {dest}")
    return True


def safe_name(rel):
    return bool(rel) and not os.path.isabs(rel) and ".." not in rel.replace("\\", "/").split("/")


def plan_restore(root, archive, include_computer_files):
    """-> list of (rel, action) with action in new / changed / same / skipped."""
    m = read_manifest(archive)
    if not m:
        raise SystemExit(f"{archive} is not a groundtrack backup")
    other = m.get("host") != socket.gethostname()
    plan = []
    with zipfile.ZipFile(archive) as z:
        for rel in m["files"]:
            if not safe_name(rel):
                plan.append((rel, "skipped"))
                continue
            if other and not include_computer_files and os.path.basename(rel) in COMPUTER_FILES:
                plan.append((rel, "skipped"))
                continue
            dst = os.path.join(root, rel)
            if not os.path.exists(dst):
                plan.append((rel, "new"))
            elif open(dst, "rb").read() == z.read(rel):
                plan.append((rel, "same"))
            else:
                plan.append((rel, "changed"))
    return m, other, plan


def restore(root, archive, yes, include_computer_files, say=say):
    m, other, plan = plan_restore(root, archive, include_computer_files)
    say(f"Backup {os.path.basename(archive)} - made {m.get('created')} on {m.get('host')}"
        + ("  (a DIFFERENT computer)" if other else ""))
    counts = {}
    for rel, act in plan:
        counts[act] = counts.get(act, 0) + 1
        if act != "same":
            say(f"  {act:8} {rel}")
    say("  " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items())))
    if other and not include_computer_files:
        say("  (skipped files describe the other computer's radios/stations; add --include-computer-files to restore them too)")
    todo = [rel for rel, act in plan if act in ("new", "changed")]
    if not yes:
        say("Nothing changed. To restore: add --yes" if todo else "Nothing to restore - this computer already matches.")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with zipfile.ZipFile(archive) as z:
        for rel in todo:
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                shutil.copy2(dst, f"{dst}.bak-{stamp}")
            with open(dst, "wb") as f:
                f.write(z.read(rel))
    say(f"Restored {len(todo)} file(s). Replaced files were kept as <name>.bak-{stamp}.")
    say("Next: ./regen_all.sh (or the GUI's Regenerate .grc) rebuilds the compiled flowgraphs; then python3 preflight.py.")
    return 0


WHEN = {"daily": "*-*-* 03:30:00", "weekly": "Sun *-*-* 03:30:00", "monthly": "*-*-01 03:30:00"}


def units(root, scp, every="weekly"):
    exe = f"{sys.executable} {os.path.join(root, 'backup_state.py')} --quiet"
    svc = f"""[Unit]
Description=groundtrack settings backup

[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart={exe}
"""
    timer = f"""[Unit]
Description=groundtrack settings backup, {every}

[Timer]
OnCalendar={WHEN[every]}
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
"""
    return svc, timer


def schedule(root, off, every="weekly"):
    if not shutil.which("systemctl"):
        raise SystemExit("needs systemd (systemctl) on this computer")
    d = os.path.expanduser("~/.config/systemd/user")

    def sc(*a):
        return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True)

    if off:
        sc("disable", "--now", f"{UNIT}.timer")
        for ext in ("service", "timer"):
            try:
                os.remove(os.path.join(d, f"{UNIT}.{ext}"))
            except OSError:
                pass
        sc("daemon-reload")
        say("Scheduled backup switched off (existing backups are kept).")
        return
    svc, timer = units(root, None, every)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, f"{UNIT}.service"), "w").write(svc)
    open(os.path.join(d, f"{UNIT}.timer"), "w").write(timer)
    sc("daemon-reload")
    r = sc("enable", "--now", f"{UNIT}.timer")
    if r.returncode != 0:
        raise SystemExit("could not enable the timer: " + (r.stderr or r.stdout).strip())
    say(f"Backup is on: {every} (about 03:30; if the computer was off it runs when it next starts).")
    say(f"Check:   python3 backup_state.py --check        Run it now:  systemctl --user start {UNIT}.service")


def check(root, folder):
    arcs = archives(folder)
    if not arcs:
        say(f"No backups yet in {folder}. Make one:  python3 backup_state.py")
    else:
        m = read_manifest(arcs[0]) or {}
        age = time.time() - os.path.getmtime(arcs[0])
        say(f"Newest backup: {os.path.basename(arcs[0])}  ({age / 3600:.1f} h ago, {len(m.get('files', []))} files); {len(arcs)} kept in {folder}")
    try:
        last = json.load(open(os.path.join(root, ".last_backup.json")))
        say(f"Last run: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last['time']))} - {'ok' if last['ok'] else 'PROBLEM'}: {last['note']}")
    except (OSError, ValueError, KeyError):
        pass
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "is-active", f"{UNIT}.timer"], capture_output=True, text=True)
        on = r.stdout.strip() == "active"
        say("Scheduled backup: " + ("ON" if on else "off - turn on with: python3 backup_state.py --schedule"))
    dest = load_local(root).get("scp")
    say("Copy to another machine: " + (dest if dest else "not set (--scp user@host:dir/ --remember)"))


def main():
    ap = argparse.ArgumentParser(description="Back up / restore this computer's groundtrack settings.")
    ap.add_argument("--dir", help=f"where backups go (default {DEFAULT_DIR})")
    ap.add_argument("--keep", type=int, help="how many backups to keep (default 5)")
    ap.add_argument("--scp", metavar="DEST", help="also copy each new backup to this scp destination (user@host:dir/)")
    ap.add_argument("--remember", action="store_true", help=f"save --dir/--keep/--scp in {LOCAL_CONFIG} (not committed)")
    ap.add_argument("--force", action="store_true", help="save even if nothing changed")
    ap.add_argument("--list", nargs="?", const="", metavar="FILE", help="list backups, or the contents of one")
    ap.add_argument("--restore", nargs="?", const="", metavar="FILE", help="restore the newest backup (or FILE); dry run unless --yes")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--include-computer-files", action="store_true", help="on restore, also restore sdr.local.yaml / computer.local.yaml")
    ap.add_argument("--schedule", nargs="?", const="weekly", choices=sorted(WHEN), metavar="WHEN",
                    help="back up automatically: weekly (default), daily or monthly")
    ap.add_argument("--unschedule", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="print only problems (for the scheduled run)")
    args = ap.parse_args()

    root = HERE
    cfg = load_local(root)
    if args.remember:
        for k, v in (("dir", args.dir), ("keep", args.keep), ("scp", args.scp)):
            if v:
                cfg[k] = v
        with open(os.path.join(root, LOCAL_CONFIG), "w") as f:
            f.write("# This computer's backup settings (not committed - see .gitignore).\n")
            yaml.safe_dump(cfg, f, default_flow_style=False)
        say(f"Remembered in {LOCAL_CONFIG}.")
    folder = backup_dir(root, args.dir)
    keep = args.keep or int(cfg.get("keep") or 5)
    dest = args.scp or cfg.get("scp")

    if args.schedule or args.unschedule:
        schedule(root, args.unschedule, args.schedule or "weekly")
        return 0
    if args.check:
        check(root, folder)
        return 0
    if args.list is not None:
        if args.list:
            m = read_manifest(args.list)
            if not m:
                raise SystemExit(f"{args.list} is not a groundtrack backup")
            say(f"{os.path.basename(args.list)}: made {m['created']} on {m['host']}, {len(m['files'])} files")
            for rel in m["files"]:
                say("  " + rel)
        else:
            for p in archives(folder) or []:
                m = read_manifest(p) or {}
                say(f"{os.path.basename(p)}  {os.path.getsize(p) / 1024:.0f} KB  {len(m.get('files', []))} files  {m.get('host', '?')}")
            if not archives(folder):
                say(f"No backups in {folder}.")
        return 0
    if args.restore is not None:
        path = args.restore or (archives(folder) or [None])[0]
        if not path:
            raise SystemExit(f"no backups in {folder}")
        return restore(root, path, args.yes, args.include_computer_files)

    quiet = (lambda *_: None) if args.quiet else say
    try:
        path, why = make_backup(root, folder, keep, args.force, say=quiet)
    except SystemExit as e:
        record_last(root, False, str(e))
        raise
    except Exception as e:  # noqa: BLE001 - any failure must be recorded, not hidden
        record_last(root, False, f"{type(e).__name__}: {e}")
        raise
    if path is None:
        quiet(f"Nothing new to back up ({why}).")
        path = (archives(folder) or [None])[0]
        copied = True  # nothing new to send...
        try:  # ...unless the last attempt to copy it elsewhere failed: try again
            failed_before = not json.load(open(os.path.join(root, ".last_backup.json"))).get("ok", True)
        except (OSError, ValueError):
            failed_before = False
        if dest and path and failed_before:
            copied = copy_out(path, dest, say=say)
    else:
        copied = True
        if dest:
            copied = copy_out(path, dest, say=say)
    record_last(root, bool(copied), why if copied else f"saved here, but the copy to {dest} failed")
    return 0 if copied else 2


if __name__ == "__main__":
    sys.exit(main())
