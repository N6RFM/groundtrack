#!/usr/bin/env python3
"""
Offline self-test for the pass-time checks (passcheck.py), the NORAD/name matcher (satnogs_name.py) and the
overlap priority rule (plan_passes.resolve_overlaps). Touches no radio, network or station files.

    python3 test_passcheck.py
"""

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

print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
