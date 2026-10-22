#!/usr/bin/env python3
"""
Offline self-test for the pass-time checks (passcheck.py), the NORAD/name matcher (satnogs_name.py) and the
overlap priority rule (plan_passes.resolve_overlaps). Touches no radio, network or station files.

    python3 test_passcheck.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import passcheck
import satnogs_name

failures = []


def expect(label, got, want):
    ok = got == want
    print(f"[ {'ok' if ok else 'FAIL'} ] {label}" + ("" if ok else f"  got {got!r}, wanted {want!r}"))
    if not ok:
        failures.append(label)


GRC = """blocks:
- name: sink
  id: gr_filerepeater_n6rfm_AdvFileSink
  parameters:
    basedir: {d}
    basefile: demo
    recordOnStart: {rec}
    sampleRate: '50000'
"""


def make(d, rec="bool(record_iq)", frozen=False):
    open(os.path.join(d, "demo.grc"), "w").write(GRC.format(d=os.path.join(d, "iq"), rec=rec))
    arg = "False" if frozen else "record_iq"
    open(os.path.join(d, "demo.py"), "w").write(
        f"x = None\nrecord_iq = 1\nx = foo.AdvFileSink(a, b, c, d, e, f, g, h, {arg}, i)\n")
    return {"name": "DEMO", "script": os.path.join(d, "demo.py")}


with tempfile.TemporaryDirectory() as d:
    cfg = make(d)
    info = passcheck.sink_info(os.path.join(d, "demo.grc"))
    expect("sink_info reads the folder", info["basedir"], os.path.join(d, "iq"))
    expect("sink_info reads the sample rate", info["sample_rate"], 50000.0)
    expect("a healthy pass has no problems", passcheck.pre_pass_problems(cfg, True, 360), [])
    expect("recording off: nothing to check", passcheck.pre_pass_problems(cfg, False, 360), [])
    frozen = make(d, frozen=True)
    probs = passcheck.pre_pass_problems(frozen, True, 360)
    expect("frozen Record On Start is reported", [s for s, m in probs if "frozen" in m], ["warn"])
    expect("...but not when recording is off", passcheck.pre_pass_problems(frozen, False, 360), [])
    expect("missing script blocks the pass",
           passcheck.pre_pass_problems({"name": "X", "script": os.path.join(d, "nope.py")}, True, 60)[0][0], "block")

    cfg = make(d)
    t0 = time.time()
    expect("no file written = MISSING", passcheck.verify_iq(cfg, True, t0, t0 + 360)["status"], "MISSING")
    expect("recording off = SKIPPED", passcheck.verify_iq(cfg, False, t0, t0 + 360)["status"], "SKIPPED")
    iq = os.path.join(d, "iq")
    open(os.path.join(iq, "wrongname_50000SPS_1Hz_x.iq"), "wb").write(b"\0" * 1000)
    expect("tiny file = SHORT (any file name)", passcheck.verify_iq(cfg, True, t0, t0 + 360)["status"], "SHORT")
    with open(os.path.join(iq, "big.iq"), "wb") as f:
        f.truncate(144_000_000)
    expect("full-size file = OK", passcheck.verify_iq(cfg, True, t0, t0 + 360)["status"], "OK")
    old = os.path.join(iq, "yesterday.iq")
    open(old, "wb").write(b"\0" * 10)
    os.utime(old, (t0 - 86400, t0 - 86400))
    expect("old files are not counted", len(passcheck.verify_iq(cfg, True, t0, t0 + 360)["files"]), 2)

for mine, theirs, want in [("GEOSCAN-1", ["Geoscan-Edelveis"], True), ("BEE-1012", ["BEE-1012"], True),
                           ("BEE-1012", ["Protostar"], False), ("ERMINAZ-2V", ["ErminazV"], True),
                           ("JAMX01", ["BY70-4"], False), ("astru_ssdv", ["ASRTU-SSDV", "asrtussdv"], False),
                           ("ASRTU-1_beam_SSDV", ["ASRTU-SSDV"], True)]:
    expect(f"name match {mine!r} vs {theirs}", satnogs_name.names_match(mine, theirs), want)

import plan_passes


def mk(name, norad, aos, los):
    return {"name": name, "norad": norad, "aos": aos, "los": los, "max_elevation_deg": 40, "approved": True}


a = mk("LOW", 1, "2026-01-01T10:00:00Z", "2026-01-01T10:10:00Z")
b = mk("HIGH", 2, "2026-01-01T10:05:00Z", "2026-01-01T10:15:00Z")
plan_passes.resolve_overlaps([a, b], False, lambda p: 4531, lambda p: {1: 0, 2: 5}[p["norad"]])
expect("higher priority keeps the overlap", (a["approved"], b["approved"]), (False, True))
a = mk("LOW", 1, "2026-01-01T10:00:00Z", "2026-01-01T10:10:00Z")
b = mk("SAME", 2, "2026-01-01T10:05:00Z", "2026-01-01T10:15:00Z")
plan_passes.resolve_overlaps([a, b], False, lambda p: 4531, lambda p: 0)
expect("equal priority still needs a choice (both stay)", (a["approved"], b["approved"]), (True, True))
a = mk("LOW", 1, "2026-01-01T10:00:00Z", "2026-01-01T10:10:00Z")
b = mk("HIGH", 2, "2026-01-01T10:05:00Z", "2026-01-01T10:15:00Z")
plan_passes.resolve_overlaps([a, b], False, lambda p: {1: 4531, 2: 4532}[p["norad"]], lambda p: {1: 0, 2: 5}[p["norad"]])
expect("different receivers are never settled by priority", (a["approved"], b["approved"]), (True, True))

# ---- pass details: direction, rise/peak/set compass (passinfo.py, plan_passes.pass_details) ----
import passinfo
expect("compass N", passinfo.compass(0), "N")
expect("compass wraps", passinfo.compass(359), "N")
expect("compass ESE", passinfo.compass(112), "ESE")
expect("track text", passinfo.track_text({"direction": "ascending", "aos_az": 135, "max_az": 82, "los_az": 40}),
       "ASC  SE 135 > E 82 > NE 40")
expect("old schedule entries give no track text", passinfo.track_text({"norad": 1}), "")

from skyfield.api import EarthSatellite, load, wgs84
_ts = load.timescale()
_iss = EarthSatellite("1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9990",
                      "2 25544  51.6400 247.4627 0006703 130.5360 325.0288 15.50000000 99999", "ISS", _ts)
_obs = wgs84.latlon(41.7167, -71.4281, 20)
_n = _wrong = 0
for _aos, _los, _mx, _ha, _hl, _d in plan_passes.find_passes(_iss, _obs, _ts.utc(2024, 1, 2), _ts.utc(2024, 1, 3), 10):
    _n += 1
    _lat = lambda t: _iss.at(t).subpoint().latitude.degrees
    _wrong += _d["direction"] != ("ascending" if _lat(_los) > _lat(_aos) else "descending")
    _wrong += not all(0 <= _d[k] <= 360 for k in ("aos_az", "max_az", "los_az"))
expect("pass details: found passes", _n > 0, True)
expect("pass details: direction matches the latitude change, azimuths in range", _wrong, 0)

# --- status.py ---
import status
from datetime import datetime, timedelta, timezone
expect("status: human 25 min", status.human(timedelta(minutes=25)), "25m")
expect("status: human 3h05", status.human(timedelta(hours=3, minutes=5)), "3h 05m")
expect("status: human 2 days", status.human(timedelta(days=2, hours=4)), "2d 4h")
_td = tempfile.mkdtemp()
_tp = os.path.join(_td, "t.txt")
open(_tp, "w").write("ISS\n1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9990\n2 25544  51.6400 247.4627 0006703 130.5360 325.0288 15.50000000 99999\n")
_ep = status.tle_epochs(_tp)
expect("status: TLE epoch read", _ep.get(25544), datetime(2024, 1, 1, 12, tzinfo=timezone.utc))
expect("status: unreadable TLE file gives {}", status.tle_epochs(os.path.join(_td, "none")), {})
_lp = os.path.join(_td, "log.jsonl")
_a = datetime.now(timezone.utc) - timedelta(hours=2)
with open(_lp, "w") as _f:
    for _e, _x in (("started", {}), ("completed", {}), ("iq_check", {"status": "SHORT", "note": "tiny"})):
        _f.write(json.dumps({"event": _e, "satellite": "S", "norad": 1, "aos": _a.isoformat(),
                             "los": (_a + timedelta(minutes=8)).isoformat(), "timestamp": _a.isoformat(), **_x}) + "\n")
_c = status.correlate_log(_lp, datetime.now(timezone.utc) - timedelta(hours=24))
expect("status: log merged to one pass", len(_c), 1)
expect("status: outcome and IQ result merged", (_c[0]["outcome"], _c[0]["iq"]), ("completed", "SHORT"))
expect("status: old passes excluded", status.correlate_log(_lp, datetime.now(timezone.utc) - timedelta(minutes=30)), [])

# --- schedule_sync.py: editing a satellite keeps the queue in step ---
import schedule_sync
import yaml as _y
from tle_number import _checksum as _cs
_cwd = os.getcwd()
_sd = tempfile.mkdtemp()
os.chdir(_sd)
os.makedirs("tle")
_now = datetime.now(timezone.utc)
_l1 = "1 25544U 98067A   %02d%012.8f  .00016717  00000-0  10270-3 0  999" % (_now.year % 100, _now.timetuple().tm_yday + (_now.hour + _now.minute / 60) / 24)
open("tle/a.txt", "w").write("ISS\n" + _l1 + _cs(_l1) + "\n2 25544  51.6400 247.4627 0006703 130.5360 325.0288 15.50000000 99999\n")
_f = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
def _cfg(**iss):
    _y.safe_dump({"ground_station": {"lat": 41.7, "lon": -71.4, "alt_m": 20}, "tle_file": "tle/a.txt", "rig_port": 4531,
                  "satellites": [dict({"name": "ISS", "norad": 25544, "freq_hz": 1, "script": "x.py", "min_elev_deg": 10}, **iss),
                                 {"name": "OTHER", "norad": 99999, "freq_hz": 1, "script": "y.py", "min_elev_deg": 10}]},
                 open("satellites.yaml", "w"))
def _sched():
    return _y.safe_load(open("schedule.yaml"))["passes"]
_keep = {"name": "OTHER", "norad": 99999, "aos": _f(_now + timedelta(hours=2)), "los": _f(_now + timedelta(hours=2, minutes=9)),
         "max_elevation_deg": 40.0, "approved": True}
_stale = {"name": "ISS", "norad": 11111, "aos": _f(_now + timedelta(hours=1)), "los": _f(_now + timedelta(hours=1, minutes=9)),
          "max_elevation_deg": 30.0, "approved": True}
_rider = dict(_keep, aos=_f(_now + timedelta(hours=5)), los=_f(_now + timedelta(hours=5, minutes=9)),
              rides_with="11111@" + _stale["aos"])
_cfg()
expect("sync: no schedule.yaml, nothing to do", schedule_sync.sync({25544}), "")
_y.safe_dump({"passes": [_stale, _keep, _rider]}, open("schedule.yaml", "w"), sort_keys=False)
_msg = schedule_sync.sync({25544, 11111})
_p = _sched()
expect("sync: stale NORAD's passes removed", [x for x in _p if x["norad"] == 11111], [])
expect("sync: the changed satellite's passes are planned", len([x for x in _p if x["norad"] == 25544]) > 0, True)
expect("sync: new passes approved with track details", all(x["approved"] and "direction" in x for x in _p if x["norad"] == 25544), True)
expect("sync: an unrelated satellite's pass is untouched", [x for x in _p if x["norad"] == 99999 and x["aos"] == _keep["aos"]], [_keep])
expect("sync: a rider whose leader vanished is freed", [x.get("rides_with") for x in _p if x["norad"] == 99999 and x["aos"] == _rider["aos"]], [None])
expect("sync: queue stays sorted", [x["aos"] for x in _p], sorted(x["aos"] for x in _p))
expect("sync: reports what it did", "new pass" in _msg and "removed" in _msg, True)
_cfg(enabled=False)
schedule_sync.sync({25544})
expect("sync: disabling a satellite drops its passes", [x for x in _sched() if x["norad"] == 25544], [])
expect("sync: nothing to change gives no output", schedule_sync.sync({25544}), "")
os.chdir(_cwd)

print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
