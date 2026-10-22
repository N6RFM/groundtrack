#!/usr/bin/env python3
"""
status.py - one screen that answers "what's next, what recorded lately, is anything wrong?"
for every station that runs on THIS computer.

    python3 status.py                 # the stations remembered in computer.local.yaml (all, if none)
    python3 status.py --radio helix   # just one
    python3 status.py --hours 48      # look further back for "what recorded"
    python3 status.py --radios        # also look for the SDRs on USB (takes a few seconds)

Per station it shows:
    - whether run_passes.py is running, and when the last pass happened
    - the next pass (countdown, where it goes in the sky) and how many are queued after it
    - the passes of the last 24 hours: recorded OK / failed / IQ file missing or short
    - PROBLEMS: nothing to run next, run_passes not running with a pass close, a failed or empty
      recording, a stale TLE, a full IQ disk, a flowgraph that will not record

It only reads files (and checks whether a process is alive); it never changes anything, so it is
safe to run at any time, even while run_passes.py is tracking. Exit status is 1 when there is a
problem, 0 otherwise, so it can sit in a script or a cron job.

The other computer's status is that computer's own: run this on each.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import yaml

import passcheck
from passinfo import track_text

HERE = os.path.dirname(os.path.abspath(__file__))
SOON_MIN = 60            # run_passes not running with a pass this close -> problem
EMPTY_QUEUE_HOURS = 3    # fewer than this many hours of queue left -> warn the plan is running out


# ---------------------------------------------------------------- helpers

def parse_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_yaml(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None


def human(td):
    s = int(abs(td.total_seconds()))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def stations_here():
    """[(name, label, folder)] to report on: the stations remembered for this computer, else all of them."""
    radios = load_yaml(os.path.join(HERE, "radios.yaml"))
    if not radios or not radios.get("stations"):
        return [("", "", HERE)]
    all_st = {}
    for name, spec in radios["stations"].items():
        spec = spec or {}
        d = os.path.expanduser(str(spec.get("dir") or name))
        all_st[str(name)] = (str(spec.get("label") or name),
                             os.path.normpath(d if os.path.isabs(d) else os.path.join(HERE, d)))
    local = load_yaml(os.path.join(HERE, "computer.local.yaml")) or {}
    chosen = [s for s in (local.get("stations") or []) if s in all_st] or list(all_st)
    return [(n, all_st[n][0], all_st[n][1]) for n in chosen]


def running_pid(folder):
    """PID in the station's run_passes.lock if that process is alive, else None."""
    try:
        pid = int(open(os.path.join(folder, "run_passes.lock")).read().strip())
        os.kill(pid, 0)
        return pid
    except PermissionError:
        return pid
    except (OSError, ValueError):
        return None


def tle_epochs(path):
    """{norad: datetime} of every TLE in a 3-line file; {} if unreadable."""
    out = {}
    try:
        lines = [l.rstrip("\n") for l in open(path) if l.strip()]
    except OSError:
        return out
    for i in range(0, len(lines) - 2, 3):
        l1 = lines[i + 1]
        try:
            norad = int(l1[2:7])
            yy, doy = int(l1[18:20]), float(l1[20:32])
            year = 2000 + yy if yy < 57 else 1900 + yy
            out[norad] = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        except (ValueError, IndexError):
            continue
    return out


def correlate_log(path, since):
    """One entry per pass that started or ended since `since`: merges the pass_log events."""
    entries = {}
    try:
        recs = [json.loads(l) for l in open(path) if l.strip()]
    except (OSError, ValueError):
        return []
    for r in recs:
        try:
            key = (r["norad"], r["aos"])
            e = entries.setdefault(key, {"satellite": r["satellite"], "aos": parse_dt(r["aos"]),
                                         "los": parse_dt(r.get("los"))})
        except KeyError:
            continue
        ev = r.get("event")
        if ev == "started":
            e["started"] = True
        elif ev == "iq_check":
            e["iq"] = r.get("status")
            e["iq_note"] = r.get("note", "")
        elif ev == "precheck":
            e["warnings"] = r.get("problems", [])
        elif ev == "skipped":
            e["outcome"] = "skipped"
            e["detail"] = r.get("reason", "")
        elif ev in ("completed", "crashed", "error"):
            e["outcome"] = ev
            e["detail"] = r.get("reason") or r.get("error") or (
                f"exit code {r['exit_code']}" if "exit_code" in r else "")
    out = [e for e in entries.values() if e["aos"] and e["aos"] >= since]
    return sorted(out, key=lambda e: e["aos"])


# ---------------------------------------------------------------- one station

def station_report(name, label, folder, hours, now):
    """Prints one station's section; returns the number of problems found."""
    problems, notes = [], []
    title = f"{name.upper()}  {label}" if name else "Station"
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))

    cfg = load_yaml(os.path.join(folder, "satellites.yaml"))
    if cfg is None:
        print("  no satellites.yaml here - nothing to report.")
        return 1
    sats = [s for s in cfg.get("satellites", []) if s.get("enabled", True)]

    # --- is run_passes running
    pid = running_pid(folder)
    print(f"  run_passes.py : {'running (PID %d)' % pid if pid else 'NOT running'}")

    # --- next pass
    sched = load_yaml(os.path.join(folder, "schedule.yaml")) or {}
    passes = []
    for p in sched.get("passes", []) or []:
        if not p.get("approved"):
            continue
        a, l = parse_dt(p.get("aos")), parse_dt(p.get("los"))
        if a and l:
            passes.append((a, l, p))
    passes.sort(key=lambda t: t[0])
    upcoming = [t for t in passes if t[1] > now]
    if upcoming:
        a, l, p = upcoming[0]
        if a <= now:
            when = f"ACTIVE NOW (sets in {human(l - now)})"
        else:
            when = f"in {human(a - now)}"
        local_t = a.astimezone().strftime("%a %H:%M")
        tr = track_text(p)
        print(f"  next pass     : {p.get('name', '?')}  {when}  ({local_t} local, "
              f"{p.get('max_elevation_deg', '?')} deg max)" + (f"  {tr}" if tr else ""))
        print(f"  queued        : {len(upcoming)} approved pass(es), through "
              f"{upcoming[-1][1].astimezone().strftime('%a %H:%M')} local")
        if (upcoming[-1][1] - now) < timedelta(hours=EMPTY_QUEUE_HOURS):
            problems.append(f"the queue ends in {human(upcoming[-1][1] - now)} - plan more: python3 plan_passes.py"
                            + (f" --radio {name}" if name else ""))
        if not pid and (a - now) < timedelta(minutes=SOON_MIN):
            problems.append(f"a pass starts in {human(max(a - now, timedelta(0)))} but run_passes.py is not running - "
                            f"start it (GUI: Start run_passes.py)")
    else:
        print("  next pass     : none queued")
        problems.append("no upcoming approved passes - run: python3 plan_passes.py" + (f" --radio {name}" if name else ""))

    # --- recent passes
    since = now - timedelta(hours=hours)
    log = correlate_log(os.path.join(folder, "pass_log.jsonl"), since)
    if log:
        print(f"  last {hours:g} h      : {len(log)} pass(es)")
        bad = 0
        for e in log:
            outcome = e.get("outcome")
            iq = e.get("iq")
            ended = e["los"] and e["los"] < now
            if outcome in ("crashed", "error"):
                mark, bad = "FAILED ", bad + 1
            elif iq in ("MISSING", "SHORT"):
                mark, bad = f"IQ {iq}", bad + 1
            elif outcome == "completed" and iq in ("OK", "SKIPPED", None, "UNKNOWN"):
                mark = "ok     "
            elif outcome == "skipped":
                mark = "skipped"
            elif not ended:
                mark = "running"
            else:
                mark, bad = "NO END ", bad + 1
            extra = ""
            if iq == "OK":
                extra = f"  IQ {e.get('iq_note', '')}"
            elif iq in ("MISSING", "SHORT"):
                extra = f"  {e.get('iq_note', '')}"
            elif e.get("detail"):
                extra = f"  {e['detail']}"
            if e.get("warnings"):
                extra += f"  [{len(e['warnings'])} pre-pass warning(s)]"
            print(f"      {mark:<10} {e['aos'].astimezone().strftime('%a %H:%M')}  {e['satellite']:<14}{extra}")
        if bad:
            problems.append(f"{bad} of the last {len(log)} pass(es) did not record properly - see: "
                            f"python3 show_pass_log.py" + (f" --radio {name}" if name else "") + " --failures-only")
    else:
        print(f"  last {hours:g} h      : no passes logged")

    # --- the TLE behind the schedule
    epochs = {}
    for key in ("tle_file", "custom_tle_file"):
        if cfg.get(key):
            epochs.update(tle_epochs(os.path.normpath(os.path.join(folder, str(cfg[key])))))
    ages = [(now - epochs[s["norad"]]).total_seconds() / 86400 for s in sats if s.get("norad") in epochs]
    if ages:
        oldest = max(ages)
        print(f"  TLE age       : newest {min(ages):.1f} d, oldest {oldest:.1f} d (of {len(ages)} satellites)")
        if oldest > passcheck.STALE_TLE_DAYS:
            n = sum(1 for a in ages if a > passcheck.STALE_TLE_DAYS)
            problems.append(f"{n} satellite(s) have a TLE older than {passcheck.STALE_TLE_DAYS} days - run: python3 update_tle.py")

    # --- IQ folder space, and compiled flowgraphs that would not record
    checked_dirs, frozen = set(), []
    for s in sats:
        grc = passcheck.grc_text_for(s)
        script = os.path.join(folder, str(s.get("script", "")))
        sink = passcheck.sink_info(os.path.join(folder, grc)) if grc and os.path.exists(os.path.join(folder, grc)) else None
        if not sink:
            continue
        d = sink.get("basedir")
        if d and os.path.isdir(d) and d not in checked_dirs:
            checked_dirs.add(d)
            free = shutil.disk_usage(d).free
            print(f"  IQ disk       : {free / 1e9:.0f} GB free in {d}")
            if free < passcheck.MIN_FREE_BYTES * 4:
                problems.append(f"only {free / 1e9:.1f} GB free in {d} - IQ recordings need room")
        if os.path.exists(script) and os.path.exists(os.path.join(folder, grc)):
            fz, _ = passcheck.compiled_record_state(script, os.path.join(folder, grc))
            if fz:
                frozen.append(s["name"])
    if frozen:
        problems.append("Record On Start is frozen to False in the compiled script for: " + ", ".join(frozen)
                        + " - clear the cache: rm -rf ~/.cache/grc_gnuradio, then recompile")

    # --- verdict
    if problems:
        print("  PROBLEMS:")
        for p in problems:
            print(f"    ! {p}")
    else:
        print("  all clear")
    return len(problems)


def radios_report():
    print("\n=== SDRs on USB " + "=" * 52)
    try:
        r = subprocess.run(["SoapySDRUtil", "--find"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  could not run SoapySDRUtil --find: {e}")
        return 1
    found = [l.split("=", 1)[1].strip() for l in r.stdout.splitlines() if l.strip().startswith("label =")]
    if not found:
        print("  ! no radios found")
        return 1
    for f in found:
        print(f"  {f}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--radio", help="report on this station only")
    ap.add_argument("--hours", type=float, default=24, help="how far back to look for recorded passes (default 24)")
    ap.add_argument("--radios", action="store_true", help="also look for the SDRs on USB")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    sts = stations_here()
    if args.radio:
        sts = [s for s in sts if s[0].lower() == args.radio.lower()] or \
            sys.exit(f"no station {args.radio!r} on this computer. Here: {', '.join(s[0] for s in sts)}")
    print(f"groundtrack status - {now.astimezone().strftime('%a %Y-%m-%d %H:%M %Z')}  ({len(sts)} station(s) on this computer)")
    total = sum(station_report(n, l, d, args.hours, now) for n, l, d in sts)
    if args.radios:
        total += radios_report()
    print()
    print(f"{total} problem(s)." if total else "Everything looks fine.")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
