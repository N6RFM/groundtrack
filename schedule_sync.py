#!/usr/bin/env python3
"""
schedule_sync.py - keep schedule.yaml in step with satellites.yaml after a satellite is edited, so
nobody has to remember to "run plan passes" to repair the queue.

A library, called by edit_satellite.py, toggle_satellite.py and delete_satellite.py once they have saved
their change to satellites.yaml (they have already chosen the station folder, so the relative paths here
are that station's). It touches only what the change affects:

    - passes whose satellite is no longer enabled or no longer exists are dropped
    - passes of a satellite whose NORAD, minimum elevation or enabled state changed are recomputed
      (over the same stretch of time the existing queue covers, at least 24 hours)
    - every other pass - and its approval, and any pairing - is left exactly as it was

No schedule.yaml yet means nothing to fix, so nothing is written. Nothing here raises: a failure is
reported as text and the person is told to run plan_passes.py, never left with a half-changed edit.
"""

import math
import os
from datetime import datetime, timedelta, timezone

import yaml

CONFIG_PATH = "satellites.yaml"
SCHEDULE_PATH = "schedule.yaml"
MIN_HOURS = 24
MAX_HOURS = 168


def _iso(v):
    """A schedule time as the 'YYYY-MM-DDTHH:MM:SSZ' string plan_passes writes."""
    if isinstance(v, datetime):
        v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(v)


def _dt(s):
    return datetime.strptime(_iso(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def sync(changed_norads=(), now=None):
    """Brings schedule.yaml in line with satellites.yaml. Returns a one-line summary, or "" when there was
    nothing to do. `changed_norads`: NORADs (old and new) whose passes must be recomputed."""
    try:
        return _sync(set(int(n) for n in changed_norads), now)
    except Exception as e:      # never break the edit that called this
        return (f"Could not update schedule.yaml automatically ({type(e).__name__}: {e}) - "
                f"run: python3 plan_passes.py")


def _sync(changed, now):
    if not os.path.exists(SCHEDULE_PATH):
        return ""
    with open(SCHEDULE_PATH) as f:
        old = (yaml.safe_load(f) or {}).get("passes") or []
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    enabled = {int(s["norad"]): s for s in cfg.get("satellites", []) if s.get("enabled", True)}
    now = now or datetime.now(timezone.utc)

    kept, dropped = [], 0
    for p in old:
        n = int(p.get("norad", -1))
        if n in enabled and n not in changed:
            kept.append(p)
        else:
            dropped += 1

    # a rider whose leader just went away goes back to taking its turn on its own
    refs = {f"{p.get('norad')}@{_iso(p.get('aos'))}" for p in kept}
    for p in kept:
        if p.get("rides_with") and p["rides_with"] not in refs:
            p.pop("rides_with", None)

    # how far ahead the queue reached, so the recomputed passes cover the same stretch
    los_times = [_dt(p["los"]) for p in old if p.get("los")]
    hours = MIN_HOURS
    if los_times:
        hours = min(MAX_HOURS, max(MIN_HOURS, math.ceil((max(los_times) - now).total_seconds() / 3600)))

    added, missing_tle = [], []
    want = sorted(n for n in changed if n in enabled)
    if want:
        import lanes
        import plan_passes
        from skyfield.api import load, wgs84
        ts = load.timescale()
        gs = cfg["ground_station"]
        observer = wgs84.latlon(gs["lat"], gs["lon"], gs["alt_m"])
        tles = plan_passes.load_tles(cfg, set(want))
        t0 = ts.from_datetime(now)
        t1 = ts.from_datetime(now + timedelta(hours=hours))
        for n in want:
            sat = tles.get(n)
            if sat is None:
                missing_tle.append(enabled[n]["name"])
                continue
            for aos, los, max_el, _ha, _hl, details in plan_passes.find_passes(
                    sat, observer, t0, t1, enabled[n].get("min_elev_deg", 15)):
                added.append({"name": enabled[n]["name"], "norad": n, "aos": aos.utc_iso(), "los": los.utc_iso(),
                              "max_elevation_deg": float(round(max_el, 1)), **details, "approved": True})
        if added:
            merged = kept + added
            lane = cfg.get("rig_port")
            sat_lane = {n: lanes.doppler_port(c, lane) for n, c in enabled.items()}
            prio = {n: int(c.get("priority", 0) or 0) for n, c in enabled.items()}
            plan_passes.resolve_overlaps(merged, False, lambda p: sat_lane.get(p["norad"], lane),
                                         lambda p: prio.get(p["norad"], 0))
            kept = merged

    kept.sort(key=lambda p: _iso(p.get("aos")))
    if not (dropped or added):
        return ""
    with open(SCHEDULE_PATH, "w") as f:
        yaml.dump({"passes": [{k: v for k, v in p.items() if not str(k).startswith("_")} for p in kept]},
                  f, sort_keys=False, default_flow_style=False)
    bits = []
    if dropped:
        bits.append(f"{dropped} stale pass(es) removed")
    if added:
        bits.append(f"{len(added)} new pass(es) planned for the next {hours} h")
    if missing_tle:
        bits.append(f"no TLE for {', '.join(missing_tle)} - run python3 update_tle.py, then plan_passes.py")
    return "schedule.yaml updated: " + ", ".join(bits) + "."
