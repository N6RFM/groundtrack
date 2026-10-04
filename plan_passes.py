#!/usr/bin/env python3
"""
Pass planner: predicts upcoming passes for every satellite in
satellites.yaml and lets you approve/reject each one before it's ever
recorded. Writes the result to schedule.yaml, which run_passes.py reads.

Usage:
    python3 plan_passes.py                    # next 24h, list + write, all approved
    python3 plan_passes.py --hours 48
    python3 plan_passes.py --interactive       # prompt y/n per pass
    python3 plan_passes.py --add-satellite     # append a new satellite entry
"""

import argparse
import os
import yaml
from datetime import datetime, timezone
from skyfield.api import load, wgs84, EarthSatellite

CONFIG_PATH = "satellites.yaml"
SCHEDULE_PATH = "schedule.yaml"


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)


def load_tles(cfg, wanted_norads):
    """Which TLE each satellite uses - decided in tle_util.py, shared with the other scripts
    so they can't disagree. A NORAD listed in custom_tle_file always uses that entry."""
    from tle_util import load_tles as pick
    return pick(cfg, wanted_norads)


def find_passes(sat, observer, t0, t1, min_elev_deg):
    """Yields (aos, los, max_elev_deg, horizon_aos, horizon_los) for every
    pass of `sat` above min_elev_deg between t0 and t1.

    horizon_aos/horizon_los are the same pass's TRUE 0-degree horizon
    rise/set times -- purely informational, for comparing against tools
    like GPredict whose pass tables report the horizon crossing rather
    than a configured minimum-elevation threshold. They are NOT used for
    scheduling: what actually gets recorded is still gated by aos/los at
    min_elev_deg, exactly as before.
    """
    times, events = sat.find_events(observer, t0, t1, altitude_degrees=min_elev_deg)

    # Same satellite, same window, at the true horizon -- used only to look
    # up which horizon-to-horizon arc each min_elev_deg pass falls inside.
    h_times, h_events = sat.find_events(observer, t0, t1, altitude_degrees=0.0)
    horizon_windows = []
    h_aos = None
    for t, ev in zip(h_times, h_events):
        if ev == 0:                      # rise (0 deg)
            h_aos = t
        elif ev == 2 and h_aos is not None:  # set (0 deg)
            horizon_windows.append((h_aos, t))
            h_aos = None

    def enclosing_horizon(aos, los):
        for h_aos, h_los in horizon_windows:
            if h_aos.tt <= aos.tt and los.tt <= h_los.tt:
                return h_aos, h_los
        return None, None  # shouldn't happen, but don't crash display if it does

    aos = None
    max_el = None
    for t, ev in zip(times, events):
        if ev == 0:          # rise
            aos, max_el = t, None
        elif ev == 1:        # culminate
            el, _, _ = (sat - observer).at(t).altaz()
            max_el = el.degrees
        elif ev == 2 and aos is not None:  # set
            h_aos, h_los = enclosing_horizon(aos, t)
            yield aos, t, max_el if max_el is not None else 0.0, h_aos, h_los
            aos = None


def find_overlaps(passes):
    """Yield (a, b) for every pair of currently-approved passes, sorted by
    aos, whose [aos, los) windows overlap - the case where the single SDR
    can only actually record whichever one starts first."""
    approved = sorted((p for p in passes if p["approved"]), key=lambda p: p["aos"])
    for i in range(len(approved) - 1):
        a, b = approved[i], approved[i + 1]
        if b["aos"] < a["los"]:
            yield a, b


def resolve_overlaps(passes, interactive):
    """Detect overlapping approved passes and either let the user actively
    choose which one to keep (interactive), or print a clear warning so it's
    not discovered silently later (non-interactive) - see the overlap
    caveat this replaces."""
    conflicts = list(find_overlaps(passes))
    if not conflicts:
        return

    print(f"\n{len(conflicts)} overlap(s) among approved passes - only one "
          f"satellite can record at a time (single SDR, no pre-emption):\n")
    for a, b in conflicts:
        if not a["approved"] or not b["approved"]:
            continue  # already resolved by an earlier conflict in this same run
        overlap_s = (parse_iso(a["los"]) - parse_iso(b["aos"])).total_seconds()
        print(f"  {a['name']:<10} {a['aos']} -> {a['los']}  (max el {a['max_elevation_deg']})")
        print(f"  {b['name']:<10} {b['aos']} -> {b['los']}  (max el {b['max_elevation_deg']})")
        print(f"  -> {overlap_s:.0f}s overlap. Without a choice, {a['name']} "
              f"wins (starts first) and {b['name']} will be skipped or "
              f"badly truncated.")

        if not interactive:
            print(f"  Re-run with --interactive to choose, or hand-edit "
                  f"{SCHEDULE_PATH} to set one side's approved: false.\n")
            continue

        while True:
            choice = input(f"  Keep which? [1] {a['name']}  [2] {b['name']}  "
                            f"[3] both anyway  [4] neither: ").strip()
            if choice == "1":
                b["approved"] = False
                break
            elif choice == "2":
                a["approved"] = False
                break
            elif choice == "3":
                break
            elif choice == "4":
                a["approved"] = False
                b["approved"] = False
                break
            else:
                print("  Please enter 1, 2, 3, or 4.")
        print()


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0,
                     help="lookahead window in hours (default 24)")
    ap.add_argument("--interactive", action="store_true",
                     help="prompt y/n for each pass instead of approving all")
    ap.add_argument("--add-satellite", action="store_true",
                     help="interactively append a new satellite to satellites.yaml and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.add_satellite:
        add_satellite(cfg)
        return

    ts = load.timescale()
    gs = cfg["ground_station"]
    observer = wgs84.latlon(gs["lat"], gs["lon"], gs["alt_m"])
    sat_cfgs = {c["norad"]: c for c in cfg["satellites"] if c.get("enabled", True)}
    tles = load_tles(cfg, set(sat_cfgs))

    missing = set(sat_cfgs) - set(tles)
    if missing:
        print(f"WARNING: no TLE for norad(s) {missing}")

    t0 = ts.now()
    t1 = ts.tt_jd(t0.tt + args.hours / 24.0)

    all_passes = []
    for norad, sat_cfg in sat_cfgs.items():
        sat = tles.get(norad)
        if sat is None:
            continue
        for aos, los, max_el, h_aos, h_los in find_passes(sat, observer, t0, t1, sat_cfg["min_elev_deg"]):
            all_passes.append({
                "name": sat_cfg["name"],
                "norad": int(norad),
                "aos": aos.utc_iso(),
                "los": los.utc_iso(),
                "max_elevation_deg": float(round(max_el, 1)),
                "approved": True,
                # informational only (leading underscore) -- the true 0-degree
                # horizon crossing for this same pass, for comparing against
                # tools like GPredict that report horizon times rather than
                # a configured threshold. Stripped out before schedule.yaml
                # is written; does not affect what gets recorded.
                "_horizon_aos": h_aos.utc_iso() if h_aos is not None else None,
                "_horizon_los": h_los.utc_iso() if h_los is not None else None,
            })

    all_passes.sort(key=lambda p: p["aos"])

    print(f"\n{len(all_passes)} pass(es) in the next {args.hours:.0f}h "
          f"above each satellite's min_elev_deg:\n")
    print(f"       {'AOS (threshold)':<22}{'LOS (threshold)':<22}{'':<14}"
          f"{'max el':<10}{'AOS (0deg)':<22}{'LOS (0deg)':<22}")
    for i, p in enumerate(all_passes):
        h_aos = p["_horizon_aos"] or "?"
        h_los = p["_horizon_los"] or "?"
        print(f"  [{i:2d}] {p['aos']:<22}{p['los']:<22}{p['name']:<14}"
              f"{p['max_elevation_deg']:>5.1f} deg  {h_aos:<22}{h_los:<22}")

    if args.interactive:
        for p in all_passes:
            ans = input(f"Record {p['name']} at {p['aos']} "
                         f"(max el {p['max_elevation_deg']})? [Y/n] ").strip().lower()
            p["approved"] = not ans.startswith("n")

    resolve_overlaps(all_passes, args.interactive)

    # strip the informational (leading-underscore) horizon fields before
    # writing schedule.yaml -- run_passes.py only ever needs aos/los at the
    # configured threshold; the 0-degree times were for the printout above.
    passes_to_write = [{k: v for k, v in p.items() if not k.startswith("_")}
                        for p in all_passes]

    with open(SCHEDULE_PATH, "w") as f:
        yaml.dump({"passes": passes_to_write}, f, sort_keys=False, default_flow_style=False)

    n_approved = sum(p["approved"] for p in all_passes)
    print(f"\nWrote {SCHEDULE_PATH}: {n_approved}/{len(all_passes)} approved.")
    if not args.interactive:
        print(f"Edit {SCHEDULE_PATH} directly (set approved: false) to skip any of them, "
              f"or rerun with --interactive to be asked about each one.")


def add_satellite(cfg):
    name = input("Satellite name (e.g. GEOSCAN-3): ").strip()
    norad = int(input("NORAD ID: ").strip())
    freq_hz = int(input("Downlink frequency (Hz): ").strip())
    script = input(f"Flowgraph script path [flowgraphs/{name.lower().replace('-', '')}.py]: ").strip() \
        or f"flowgraphs/{name.lower().replace('-', '')}.py"
    min_elev = float(input("Minimum elevation to record (deg) [15]: ").strip() or "15")
    used_ports = ({s["producer_port"] for s in cfg.get("satellites", []) if "producer_port" in s}
                  | {s["consumer_port"] for s in cfg.get("satellites", []) if "consumer_port" in s})
    import station
    used_ports |= station.other_stations_ports()   # empty unless multi-station
    producer_port, consumer_port = 9101, 8101
    while producer_port in used_ports:
        producer_port += 1
    while consumer_port in used_ports:
        consumer_port += 1
    record_iq = input("Will this satellite's .grc be wired for the record_iq "
                      "toggle? [y/N]: ").strip().lower() == "y"
    entry = {
        "name": name, "norad": norad, "freq_hz": freq_hz, "script": script,
        "min_elev_deg": min_elev, "producer_port": producer_port,
        "consumer_port": consumer_port,
        # Nothing is built or wired yet - a MISSING enabled key is treated
        # as enabled: true everywhere else in this toolkit (preflight.py,
        # run_passes.py, doctor.py), which would make this satellite look
        # live immediately and fail preflight on a .grc/.py that doesn't
        # exist. Always write it explicitly, same as add_satellite.py.
        "enabled": False,
    }
    if record_iq:
        entry["record_iq_toggle"] = True
    cfg.setdefault("satellites", []).append(entry)
    save_config(cfg)
    print(f"Added {name}: producer_port={producer_port}, consumer_port={consumer_port}")
    print(f"Added as disabled (nothing is built yet) - enable later with: "
          f"python3 toggle_satellite.py --enable {name}")
    print(f"Still needed: build flowgraphs/{name.lower().replace('-', '')}.grc "
          f"yourself in GRC, then grcc it"
          + (f", then python3 wire_record_iq.py {name} (record_iq_toggle is set, "
             f"but the .grc isn't wired for it yet)" if record_iq else "")
          + f", then python3 preflight.py.")


if __name__ == "__main__":
    main()
