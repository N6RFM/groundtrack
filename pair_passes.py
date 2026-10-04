#!/usr/bin/env python3
"""
Record two satellites at once: pair their passes.

Two approved passes that overlap in time and sit on DIFFERENT receivers - say the 70 cm
Airspy and the 2 m RTL-SDR - can be recorded together. That only makes sense when the
satellites are close together in the sky, because the beam points one way, so it's your
call, one pair of passes at a time. You also choose whose TLE steers the beam (the leader);
the other (the companion) rides along on its own receiver with its own, independent Doppler.

    python3 pair_passes.py                           # list the candidates, then choose
    python3 pair_passes.py --list
    python3 pair_passes.py --pair 2 --leader 1       # pair #2; the first satellite listed steers
    python3 pair_passes.py --pair 2 --leader HADES-L # ... or name the one that steers
    python3 pair_passes.py --unpair 2
    python3 pair_passes.py --unpair all

Overlapping passes that aren't paired are left as they always were: the one that starts
first records, the other waits. run_passes.py reads schedule.yaml when it starts, so restart
it for a change to take effect. (plan_passes.py --interactive offers the same choice as it
finds the overlaps.)
"""

import argparse
import os
import sys

import yaml

import lanes

SCHEDULE_PATH = "schedule.yaml"
CONFIG_PATH = "satellites.yaml"


def running_pid():
    """PID of a run_passes.py working in this station, or None."""
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


def load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        with open(SCHEDULE_PATH) as f:
            sched = yaml.safe_load(f) or {}
    except OSError as e:
        sys.exit(f"{e.filename}: {e.strerror}. Run plan_passes.py first.")
    sats = {c["norad"]: c for c in cfg.get("satellites", []) if c.get("enabled", True)}
    station_lane = cfg.get("rig_port")
    sat_lane = {n: lanes.doppler_port(c, station_lane) for n, c in sats.items()}
    passes = sched.get("passes") or []
    return sched, passes, (lambda p: sat_lane.get(p["norad"], station_lane))


def when(s):
    return str(s).replace("T", " ").replace("Z", "")


def show(cands, lane_of):
    if not cands:
        print("No overlapping passes on different receivers - nothing to pair.\n"
              "(Passes on the same receiver can't be recorded together: one SDR.)")
        return
    print(f"{len(cands)} overlapping pass pair(s) on different receivers:\n")
    for n, (a, b) in enumerate(cands, 1):
        print(f"  [{n}] {a['name']:<10} {when(a['aos'])} -> {when(a['los'])[11:]}  "
              f"receiver {lane_of(a)}, max el {a.get('max_elevation_deg', '?')}")
        print(f"      {b['name']:<10} {when(b['aos'])} -> {when(b['los'])[11:]}  "
              f"receiver {lane_of(b)}, max el {b.get('max_elevation_deg', '?')}   "
              f"({lanes.overlap_s(a, b):.0f}s overlap)")
        if b.get("rides_with") == lanes.pass_ref(a):
            print(f"      PAIRED: {a['name']} steers the beam; {b['name']} rides along")
        elif a.get("rides_with") == lanes.pass_ref(b):
            print(f"      PAIRED: {b['name']} steers the beam; {a['name']} rides along")
        else:
            print("      not paired - the one that starts first records, the other waits")
    print()


def leader_from(spec, a, b):
    """1/2 (the order listed), or a satellite's name. None if it doesn't pick one of them."""
    spec = str(spec).strip()
    if spec == "1":
        return a
    if spec == "2":
        return b
    named = [p for p in (a, b) if p["name"].lower() == spec.lower()]
    return named[0] if len(named) == 1 else None


def save(sched, passes):
    sched["passes"] = passes
    with open(SCHEDULE_PATH, "w") as f:
        yaml.dump(sched, f, sort_keys=False, default_flow_style=False)


def do_pair(cands, passes, lane_of, n, leader_spec):
    if not 1 <= n <= len(cands):
        return f"no candidate [{n}] - there are {len(cands)}"
    a, b = cands[n - 1]
    leader = leader_from(leader_spec, a, b)
    if leader is None:
        return f"say which satellite steers the beam: 1 ({a['name']}), 2 ({b['name']}), or its name"
    problem = lanes.pairing_problem(a, b, leader, passes, lane_of)
    if problem:
        return problem
    lanes.set_pair(a, b, leader)
    print(f"Paired: {leader['name']} steers the beam; {(b if leader is a else a)['name']} "
          f"records alongside on its own receiver.")
    return None


def do_unpair(cands, passes, n):
    if n == "all":
        cleared = 0
        for p in passes:
            if p.pop("rides_with", None) is not None:
                cleared += 1
        print(f"Cleared {cleared} pairing(s)." if cleared else "Nothing was paired.")
        return None
    n = int(n)
    if not 1 <= n <= len(cands):
        return f"no candidate [{n}] - there are {len(cands)}"
    a, b = cands[n - 1]
    if lanes.clear_pair(a, passes) + lanes.clear_pair(b, passes):
        print("Unpaired.")
    else:
        print("That pair wasn't paired.")
    return None


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show the candidates and any pairings, then stop")
    ap.add_argument("--pair", type=int, metavar="N", help="pair candidate N (needs --leader)")
    ap.add_argument("--leader", metavar="1|2|NAME", help="which satellite steers the beam")
    ap.add_argument("--unpair", metavar="N|all", help="undo pairing N, or every pairing")
    args = ap.parse_args()
    if args.unpair not in (None, "all") and not args.unpair.isdigit():
        sys.exit("--unpair takes a candidate's number, or 'all'")
    if args.pair is not None and not args.leader:
        sys.exit("--pair needs --leader (1, 2, or the satellite's name)")

    sched, passes, lane_of = load()
    cands = lanes.candidate_pairs(passes, lane_of)
    modified = False

    if args.pair is not None or args.unpair is not None:
        err = (do_pair(cands, passes, lane_of, args.pair, args.leader) if args.pair is not None
               else do_unpair(cands, passes, args.unpair))
        if err:
            sys.exit(f"Can't do that: {err}")
        save(sched, passes)
        modified = True
    elif args.list or not sys.stdin.isatty():
        show(cands, lane_of)
    else:
        while True:
            show(cands, lane_of)
            if not cands:
                break
            ans = input("Pair which? [number]  u<number> to unpair  q to quit: ").strip().lower()
            if ans in ("", "q"):
                break
            if ans.startswith("u") and ans[1:].isdigit():
                err = do_unpair(cands, passes, ans[1:])
            elif ans.isdigit() and 1 <= int(ans) <= len(cands):
                a, b = cands[int(ans) - 1]
                who = input(f"  Which steers the beam? [1] {a['name']}  [2] {b['name']}: ").strip()
                err = do_pair(cands, passes, lane_of, int(ans), who)
            else:
                err = "enter a candidate's number, u<number>, or q"
            if err:
                print(f"  Can't do that: {err}\n")
            else:
                save(sched, passes)
                modified = True
                print()

    pid = running_pid() if modified else None
    if pid:
        print(f"NOTE: run_passes.py is running (PID {pid}). It read schedule.yaml when it started - "
              f"restart it for this change to take effect.")


if __name__ == "__main__":
    main()
