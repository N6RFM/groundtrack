#!/usr/bin/env python3
"""
GEOSCAN fleet pass EXECUTOR.

Unlike the earlier version, this no longer decides on its own which passes
to record - it only acts on schedule.yaml, which plan_passes.py generates
and which you (or --interactive) have approved in advance. For each
approved pass it:

  - waits for wall-clock time to reach `aos`
  - launches the matching flowgraph subprocess
  - feeds it live Doppler-corrected frequency via the shared rigctld
  - stops it at `los` (or earlier, as a safety net, if the satellite's
    actual elevation drops back below min_elev_deg first - e.g. because
    the TLE has drifted since planning)

Run `python3 plan_passes.py` first to (re)generate schedule.yaml.
"""

import subprocess
import socket
import time
import sys
import atexit
import argparse
import shutil
import yaml
import os
import errno
import json
from datetime import datetime, timedelta, timezone
from skyfield.api import load, wgs84, EarthSatellite

CONFIG_PATH = "satellites.yaml"
SCHEDULE_PATH = "schedule.yaml"
PASS_LOG_PATH = "pass_log.jsonl"
SPEED_OF_LIGHT = 299792458.0
LOCK_PATH = "run_passes.lock"


def notify(title, message):
    """Best-effort desktop notification - silently does nothing if
    notify-send isn't available (e.g. a headless/remote box with no
    display), or if notifications aren't enabled in satellites.yaml."""
    try:
        subprocess.run(["notify-send", title, message], timeout=2,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def log_pass_event(event, sat_cfg, pass_info, **extra):
    """Appends one JSON record per line to pass_log.jsonl - a persistent,
    append-only history of what actually happened during each pass, not
    just what was predicted. Never raises - a logging failure should
    never take down the actual tracking loop, so any error here is
    swallowed rather than propagated.

    JSONL (one JSON object per line) rather than a single YAML/JSON
    document: safely appendable without re-reading or re-writing the
    whole file, and trivially parseable later with any tool, including
    a one-liner with jq or a simple Python loop - no special log-reading
    code needed in this project itself. events: "started" (AOS, one per
    pass), and exactly one of "completed" / "crashed" / "error" (how it
    ended). pass_info needs norad, aos_dt, los_dt - both `active_pass`
    and a raw schedule.yaml pass dict already have these, so either can
    be passed directly."""
    try:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "satellite": sat_cfg["name"],
            "norad": pass_info["norad"],
            "aos": pass_info["aos_dt"].isoformat(),
            "los": pass_info["los_dt"].isoformat(),
        }
        record.update(extra)
        with open(PASS_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # a logging problem must never take down the tracking loop


# A flowgraph that dies at launch used to be relaunched on the very next
# loop tick for the whole pass window - nothing remembered that the pass
# had already failed - producing hundreds of crash/relaunch cycles (and,
# with logging and notifications on, hundreds of log lines and desktop
# notifications) for a single bad pass. A launch failure can be genuinely
# transient (SDR briefly busy), so a few spaced-out retries are allowed,
# but bounded: after MAX_LAUNCH_ATTEMPTS the pass is given up on for good.
MAX_LAUNCH_ATTEMPTS = 3
LAUNCH_RETRY_DELAY_S = 15


def pass_key(p):
    """Identity of one queued pass - (norad, aos), the same pairing
    show_pass_log.py correlates on."""
    return (p["norad"], p["aos_dt"])


def launch_allowed(key, now, launch_attempts, finished_passes):
    """False if this pass already ended (normally, or was given up on),
    has used up its attempts, or is still inside the retry delay after a
    recent failed launch."""
    if key in finished_passes:
        return False
    state = launch_attempts.get(key)
    if state is None:
        return True
    if state["count"] >= MAX_LAUNCH_ATTEMPTS:
        return False
    return (now - state["last"]).total_seconds() >= LAUNCH_RETRY_DELAY_S


def note_launch(key, now, launch_attempts):
    """Records a launch attempt; returns which attempt number this is."""
    state = launch_attempts.setdefault(key, {"count": 0, "last": now})
    state["count"] += 1
    state["last"] = now
    return state["count"]


def crash_outcome(key, launch_attempts, finished_passes):
    """After a launch/early-exit failure: ('retry', n) if attempts remain,
    or ('give_up', n) - in which case the pass is marked finished so it's
    never launched again."""
    attempt = launch_attempts[key]["count"]
    if attempt >= MAX_LAUNCH_ATTEMPTS:
        finished_passes.add(key)
        return "give_up", attempt
    return "retry", attempt


def script_accepts_flag(script, flag, timeout_s=60):
    """Asks the compiled flowgraph itself, via its own --help, whether it
    accepts a flag - the only check that reflects what will actually
    execute (a .grc can be rewired without the .py being recompiled, or
    the reverse). GRC-generated scripts parse arguments before creating
    any Qt/SDR objects, so --help exits without touching hardware.
    Returns True/False, or None if it couldn't be determined."""
    try:
        out = subprocess.run([sys.executable, script, "--help"],
                              capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = out.stdout + out.stderr
    if "usage:" not in text:
        return None
    return flag in text


def verify_record_iq(sat_cfgs, passes, iq_capable):
    """{norad: ok} for every toggle-capable satellite with something queued: does its
    COMPILED flowgraph really accept --record-iq? A satellite can be capable (its .grc
    is wired, or record_iq_toggle: true) while the compiled script isn't - never
    recompiled after being wired. Passing the flag anyway makes every launch die
    instantly on an argparse error, so ask each script directly before the run starts
    and, if it doesn't accept the flag, launch without it (the .grc's own baked-in
    default applies) rather than crash on every pass."""
    record_iq_ok = {}
    for norad, c in sat_cfgs.items():
        if not iq_capable.get(norad):
            continue
        if not any(p["norad"] == norad for p in passes):
            continue  # nothing queued for it, no need to check
        print(f"Verifying {c['script']} accepts --record-iq ...")
        ok = script_accepts_flag(c["script"], "--record-iq")
        record_iq_ok[norad] = ok is not False
        if ok is False:
            print(f"WARNING: {c['name']} supports the IQ toggle (its .grc is wired for it, or "
                  f"record_iq_toggle: true), but {c['script']} doesn't accept --record-iq - most "
                  f"likely it wasn't recompiled after being wired: ./regen_all.sh. Launching it "
                  f"WITHOUT the flag meanwhile; its .grc's own record_iq default applies. (If the "
                  f".grc isn't actually wired: python3 wire_record_iq.py {c['name']}, or to stop "
                  f"treating it as toggle-capable: python3 edit_satellite.py {c['name']} "
                  f"--no-record-iq-toggle)")
        elif ok is None:
            print(f"NOTE: couldn't verify {c['script']}'s arguments (timed out or "
                  f"no usage text) - assuming it accepts --record-iq.")
    return record_iq_ok


def build_launch_command(sat_cfg, pass_rec, session_record_iq, iq_capable, record_iq_ok):
    """(command, record_iq value) for one pass. The value is None when --record-iq
    isn't being passed at all - which is every satellite that isn't toggle-capable,
    and any whose compiled flowgraph turned out not to accept it. A per-pass override
    in schedule.yaml (set via toggle_pass_record_iq.py) beats the session-wide
    --record-iq default."""
    cmd = [sys.executable, "-u", sat_cfg["script"]]
    record_iq_value = None
    norad = pass_rec["norad"]
    if iq_capable.get(norad, False) and record_iq_ok.get(norad, True):
        per_pass_override = pass_rec.get("record_iq")
        record_iq_value = (per_pass_override if per_pass_override is not None
                           else session_record_iq == "yes")
        cmd += ["--record-iq", "1" if record_iq_value else "0"]
    return cmd, record_iq_value


def acquire_lock():
    """Refuses to start a second run_passes.py against the same folder -
    two instances would fight over rigctld/rotctld and the SDR."""
    if os.path.exists(LOCK_PATH):
        with open(LOCK_PATH) as f:
            old_pid = f.read().strip()
        try:
            os.kill(int(old_pid), 0)  # signal 0: just checks it exists
            alive = True
        except (OSError, ValueError) as e:
            alive = isinstance(e, OSError) and e.errno == errno.EPERM
        if alive:
            sys.exit(f"run_passes.py already running (PID {old_pid}, lock file "
                      f"{LOCK_PATH}). Kill it first, or delete {LOCK_PATH} if it's "
                      f"stale (e.g. after a crash).")
        else:
            print(f"Stale lock file found (PID {old_pid} is not running) - removing it.")

    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(LOCK_PATH) and os.remove(LOCK_PATH))


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_tles(cfg, wanted_norads):
    """Which TLE each satellite uses - decided in tle_util.py, shared with the other scripts
    so they can't disagree. A NORAD listed in custom_tle_file always uses that entry."""
    from tle_util import load_tles as pick
    return pick(cfg, wanted_norads)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def format_countdown(td):
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def clear_line():
    # \x1b[K clears from cursor to end of line regardless of terminal width -
    # avoids the wrapping artifacts a fixed-width space-padded clear causes
    # when the terminal is narrower than the padding.
    sys.stdout.write("\r\x1b[K")


def write_status(text):
    cols = shutil.get_terminal_size(fallback=(100, 24)).columns
    sys.stdout.write("\r" + text[:cols - 1] + "\x1b[K")
    sys.stdout.flush()


def elevation_deg(sat, observer, t):
    el, az, _ = (sat - observer).at(t).altaz()
    return el.degrees, az.degrees


def preposition_for_next_pass(rot, passes, tles, sat_cfgs, ts, observer, now):
    """Called whenever a pass ends, for any reason - point the rotor at
    where the NEXT approved pass will actually rise, rather than leaving
    it wherever the just-finished pass happened to end. Otherwise the
    rotor has to slew from scratch right at the start of the next pass,
    exactly when signal is weakest (low elevation) and every second of
    mispointing costs the most. One-shot: doesn't track anything, just
    moves once and stops - the real tracking loop takes over normally
    once that satellite's own AOS actually arrives."""
    if rot is None:
        return
    upcoming = [p for p in passes if p["aos_dt"] > now]
    if not upcoming:
        return
    nxt = upcoming[0]
    sat = tles.get(nxt["norad"])
    if sat is None:
        return
    sat_cfg = sat_cfgs[nxt["norad"]]
    t_aos = ts.from_datetime(nxt["aos_dt"])
    el_deg, az_deg = elevation_deg(sat, observer, t_aos)
    rot.point(az_deg, el_deg)
    print(f"[{sat_cfg['name']}] pre-positioning rotor to az={az_deg:.1f} "
          f"el={el_deg:.1f} for its AOS in {format_countdown(nxt['aos_dt'] - now)}")


def range_km(sat, observer, t):
    return (sat - observer).at(t).distance().km


def doppler_hz(sat, observer, t, freq_hz, dt=0.5):
    ts = load.timescale()
    t2 = ts.tt_jd(t.tt + dt / 86400.0)
    range_rate_km_s = (range_km(sat, observer, t2) - range_km(sat, observer, t)) / dt
    return -freq_hz * (range_rate_km_s * 1000.0) / SPEED_OF_LIGHT


class Rotctld:
    """Client for a rotctld YOU start separately, pointed at your real rotor
    (or its Dummy backend for testing). This class does not launch rotctld -
    unlike rigctld's Dummy backend, a rotor daemon needs your actual serial
    port and rotor model, which only you can supply.

    Pure I/O - no throttling or lead-ahead logic here at all. That logic
    needs satellite/orbital data this class has no business knowing about,
    so it lives in find_lead_ahead_target() and maybe_update_rotor()
    instead, matching gtk-rot-ctrl.c's own separation: gpredict's rotor
    controller decides WHAT position to send, a plain rotctld client just
    sends whatever it's told."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
        self.last = None  # (az, el) last commanded, so callers can compare drift

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=3)

    def point(self, az_deg, el_deg):
        """Sends unconditionally - callers decide whether a send is
        warranted before calling this at all."""
        try:
            if self.sock is None:
                self._connect()
            self.sock.sendall(f"P {az_deg:.1f} {el_deg:.1f}\n".encode())
            self.sock.settimeout(0.5)
            self.sock.recv(64)
            self.last = (az_deg, el_deg)
        except OSError:
            self.sock = None  # reconnect next call

    def get_pos(self):
        """Queries the rotor's own actual, physical position via rotctld's
        'p' command - a genuine, separate connection from point()'s own
        persistent send socket, matching gtk-rot-ctrl.c's real design: it
        compares against the rotor's LIVE reported position, not wherever
        it was last commanded to go. This matters because a rotor that's
        still catching up from a previous command sits somewhere between
        its old and new targets - comparing against the stale commanded
        value instead compounds the lead-ahead offset on every single
        trigger, roughly doubling the effective step size. Returns (az,
        el) as floats, or None on any error - callers should fall back to
        self.last if this fails, rather than skip the update entirely."""
        try:
            with socket.create_connection((self.host, self.port), timeout=2) as s:
                s.sendall(b"p\n")
                s.settimeout(1)
                data = s.recv(256).decode(errors="replace")
            parts = data.split()
            return float(parts[0]), float(parts[1])
        except (OSError, ValueError, IndexError):
            return None


def find_lead_ahead_target(sat, observer, ts, now_dt, cur_az, cur_el,
                            threshold_deg, los_dt):
    """Ported from gtk-rot-ctrl.c (N6RFM/Gpredict_K4KDR_N6RFM), a proven,
    working implementation - binary-searches for the FURTHEST future time
    (up to los_dt) where the satellite's position still sits within
    threshold_deg of its CURRENT position (cur_az/cur_el), rather than
    sending the satellite's instantaneous current position. The comment
    in the original explains why directly: "try to lead the satellite
    some so we are not always chasing it." Sending a rotor toward a real
    lead-ahead destination gives it something to travel toward
    continuously, instead of repeatedly being redirected toward a target
    that's already slightly stale by the time each command lands - which
    is exactly the "too many commands" pattern this replaces.

    The anchor MUST be the satellite's current position, not wherever the
    rotor was last commanded - anchoring to a stale, already-passed
    position gives the search nothing meaningful to converge on, since
    every future point is monotonically further from a point already
    behind the satellite's motion.

    Bounded by los_dt (search never looks past the end of the current
    pass) or a 20-minute fallback window, matching the original's own
    choice when no pass end time is known."""
    max_lookahead_s = (los_dt - now_dt).total_seconds() if los_dt else 1200.0
    max_lookahead_s = max(max_lookahead_s, 1.0)
    time_delta_s = max_lookahead_s
    step_s = time_delta_s / 2.0
    min_step_s = 1.0  # matches the main loop's own 1-second cadence
    if step_s < min_step_s:
        step_s = min_step_s
    az_deg = cur_az
    el_deg = cur_el
    while step_s > min_step_s / 4.0:
        t_future = ts.from_datetime(now_dt + timedelta(seconds=time_delta_s))
        el_deg, az_deg = elevation_deg(sat, observer, t_future)
        daz = abs(az_deg - cur_az)
        delv = abs(el_deg - cur_el)
        exceeds = el_deg < 0 or daz > threshold_deg or delv > threshold_deg
        if exceeds:
            time_delta_s -= step_s
        else:
            time_delta_s += step_s
        step_s /= 2.0
    return az_deg, el_deg


def maybe_update_rotor(rot, sat, observer, ts, now_dt, cur_az, cur_el,
                        los_dt, threshold_deg=5.0):
    """The actual decision gpredict's threshold check makes: has the
    satellite drifted more than threshold_deg from the rotor's OWN LIVE
    reported position - not wherever it was last commanded to go? If not,
    do nothing at all - no time-based forced send exists here, matching
    the reference implementation exactly. If so, compute a lead-ahead
    target and send that instead of cur_az/cur_el.

    Comparing against a live-queried position (rather than the stale
    last-commanded value) matters concretely: a rotor still catching up
    from a previous command sits somewhere between its old and new
    targets, and comparing against the stale commanded value instead
    compounds the lead-ahead offset on every trigger - roughly doubling
    the real, physical step size the rotor actually takes between
    commands. Falls back to rot.last only if the live query fails (or
    there's no previous command yet), rather than skipping the update."""
    if rot is None:
        return
    reference = rot.get_pos() or rot.last
    if reference is not None:
        daz = abs(cur_az - reference[0])
        delv = abs(cur_el - reference[1])
        if daz <= threshold_deg and delv <= threshold_deg:
            return
        target_az, target_el = find_lead_ahead_target(
            sat, observer, ts, now_dt, cur_az, cur_el,
            threshold_deg, los_dt)
    else:
        target_az, target_el = cur_az, cur_el  # first command - just go there
    rot.point(target_az, target_el)



class Rigctld:
    def __init__(self, port):
        self.port = port
        self.proc = subprocess.Popen(["rigctld", "-m", "1", "-t", str(port)])
        atexit.register(self.stop)
        time.sleep(1)
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)

    def set_freq(self, freq_hz):
        try:
            self.sock.sendall(f"F {int(round(freq_hz))}\n".encode())
            self.sock.settimeout(0.5)
            self.sock.recv(64)
        except OSError:
            self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            self.sock.sendall(f"F {int(round(freq_hz))}\n".encode())

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true",
                     help="print each Doppler/rotor update while a pass is active")
    ap.add_argument("--status-interval", type=float, default=5.0, metavar="SECONDS",
                     help="how often to print the verbose status line during a pass "
                          "(default: 5s) - Doppler correction itself still recomputes "
                          "every second regardless; the rotor only moves when it's "
                          "actually drifted enough (lead-ahead targeting, not a fixed "
                          "cadence). Only the printed line is throttled by this flag")
    ap.add_argument("--no-preposition", action="store_true",
                     help="don't pre-position the rotor toward the next approved pass "
                          "when the current one ends - leave it wherever the pass "
                          "finished instead")
    ap.add_argument("--record-iq", choices=["yes", "no"], default="yes",
                     help="session-wide default for whether to record IQ data "
                          "(default: yes) - only actually affects satellites whose "
                          "satellites.yaml entry has record_iq_toggle: true, meaning "
                          "their .grc has a record_iq Parameter block wired up; every "
                          "other satellite launches exactly as before, unaffected")
    args = ap.parse_args()

    acquire_lock()

    cfg = load_yaml(CONFIG_PATH)
    notify_enabled = bool(cfg.get("notify", False))
    schedule = load_yaml(SCHEDULE_PATH)
    gs = cfg["ground_station"]
    observer = wgs84.latlon(gs["lat"], gs["lon"], gs["alt_m"])
    sat_cfgs = {c["norad"]: c for c in cfg["satellites"] if c.get("enabled", True)}
    for norad, sc in sat_cfgs.items():
        try:
            sc["freq_hz"] = float(sc["freq_hz"])
        except (TypeError, ValueError):
            sys.exit(f"satellites.yaml: {sc['name']}'s freq_hz ({sc['freq_hz']!r}) "
                      f"isn't a valid number - check for stray quotes around it.")
    tles = load_tles(cfg, set(sat_cfgs))

    passes = [p for p in schedule["passes"]
              if p.get("approved") and p["norad"] in sat_cfgs]
    for p in passes:
        p["aos_dt"] = parse_iso(p["aos"])
        p["los_dt"] = parse_iso(p["los"])
    passes.sort(key=lambda p: p["aos_dt"])

    print(f"{len(passes)} approved pass(es) loaded from {SCHEDULE_PATH}.")
    if not passes:
        print("Nothing approved - run plan_passes.py (optionally --interactive) first.")
        return

    rig = Rigctld(cfg["rig_port"])
    rot = None
    if "rot_host" in cfg and "rot_port" in cfg:
        rot = Rotctld(cfg["rot_host"], cfg["rot_port"])
        print(f"Antenna control enabled: rotctld at {cfg['rot_host']}:{cfg['rot_port']}")
    else:
        print("No rot_host/rot_port in satellites.yaml - antenna will not be steered.")
    ts = load.timescale()

    launch_attempts = {}      # pass_key -> {"count", "last"} - crash-retry accounting
    finished_passes = set()   # pass_keys that ended (or were given up on): never relaunch

    # which satellites may be handed --record-iq (explicit record_iq_toggle, else read from
    # the .grc), and for those with something queued whether the compiled flowgraph really
    # accepts it - see verify_record_iq
    from edit_satellite import record_iq_capable   # imported here, not at module level: this file stays loadable on its own
    iq_capable = {norad: record_iq_capable(c) for norad, c in sat_cfgs.items()}
    record_iq_ok = verify_record_iq(sat_cfgs, passes, iq_capable)

    active_pass = None
    active_proc = None
    is_tty = sys.stdout.isatty()
    last_pass_status_print = None   # throttle for the during-pass tracking line
    last_idle_status_print = None   # throttle for the "waiting for next pass" line

    print("Executor running. Ctrl-C to stop.")
    try:
        while True:
            now = datetime.now(timezone.utc)
            t = ts.now()

            if active_pass is not None:
                sat_cfg = sat_cfgs[active_pass["norad"]]
                sat = tles.get(active_pass["norad"])
                try:
                    if active_proc.poll() is not None:
                        if is_tty:
                            clear_line()
                        rc = active_proc.returncode
                        outcome, attempt = crash_outcome(
                            pass_key(active_pass), launch_attempts, finished_passes)
                        will_retry = outcome == "retry"
                        print(f"[{sat_cfg['name']}] flowgraph exited early "
                              f"(code {rc}) - check its output above. "
                              + (f"Attempt {attempt}/{MAX_LAUNCH_ATTEMPTS}; retrying in "
                                 f"{LAUNCH_RETRY_DELAY_S}s." if will_retry else
                                 f"Attempt {attempt}/{MAX_LAUNCH_ATTEMPTS}; giving up on "
                                 f"this pass - rotor/Doppler stopped for it."))
                        if notify_enabled:
                            notify("Satellite pass FAILED",
                                   f"{sat_cfg['name']} - flowgraph exited early "
                                   f"(code {rc}), attempt {attempt}/{MAX_LAUNCH_ATTEMPTS}"
                                   + ("" if will_retry else " - giving up"))
                        log_pass_event("crashed", sat_cfg, active_pass, exit_code=rc,
                                       attempt=attempt, will_retry=will_retry)
                        active_pass, active_proc = None, None
                        # only move the rotor toward the NEXT pass once we've
                        # stopped trying this one - pre-positioning away and
                        # then back on every retry would be pointless motion
                        if not will_retry and not args.no_preposition:
                            preposition_for_next_pass(rot, passes, tles, sat_cfgs, ts, observer, now)
                        time.sleep(1)
                        continue

                    el_deg, az_deg = elevation_deg(sat, observer, t)
                    past_los = now >= active_pass["los_dt"]
                    below_elev = el_deg < sat_cfg["min_elev_deg"]
                    if past_los or below_elev:
                        if is_tty:
                            clear_line()
                        print(f"[{sat_cfg['name']}] LOS ({'scheduled' if past_los else 'elevation safety net'})")
                        if notify_enabled:
                            notify("Satellite pass ended", f"{sat_cfg['name']} - LOS")
                        log_pass_event("completed", sat_cfg, active_pass,
                                       reason="scheduled" if past_los else "elevation_safety_net")
                        active_proc.terminate()
                        try:
                            active_proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            active_proc.kill()
                        # this pass is over - without this, an elevation
                        # safety-net LOS (which fires BEFORE the scheduled
                        # window closes) would see the pass still "in
                        # window" and relaunch it immediately
                        finished_passes.add(pass_key(active_pass))
                        active_pass, active_proc = None, None
                        if not args.no_preposition:
                            preposition_for_next_pass(rot, passes, tles, sat_cfgs, ts, observer, now)
                    else:
                        dop = doppler_hz(sat, observer, t, sat_cfg["freq_hz"])
                        corrected = sat_cfg["freq_hz"] + dop
                        rig.set_freq(corrected)
                        maybe_update_rotor(rot, sat, observer, ts, now,
                                            az_deg, el_deg, active_pass["los_dt"],
                                            threshold_deg=cfg.get("rot_threshold_deg", 5.0))
                        if args.verbose:
                            remaining = format_countdown(active_pass["los_dt"] - now)
                            status = (f"[{sat_cfg['name']}] el={el_deg:5.1f} az={az_deg:5.1f}  "
                                      f"freq={corrected:,.0f} Hz (doppler {dop:+.0f} Hz)  "
                                      f"LOS in {remaining}")
                            # Doppler correction above still recomputes every
                            # second regardless; the rotor only actually moves
                            # when it's drifted enough to warrant a new
                            # lead-ahead target, not on a fixed cadence. Only
                            # how often this STATUS LINE gets WRITTEN is
                            # throttled here, for both paths. For a real terminal, write_status()
                            # only overwrites the same line, but a terminal
                            # emulator's own scrollback/copy buffer can still
                            # preserve every individual \r-updated write as its
                            # own line - so a live TTY session copied to a text
                            # file can look like scrolling spam even though the
                            # on-screen display only ever showed one line
                            # changing. Throttling how often write_status() is
                            # even called fixes that too, not just the
                            # redirected-to-a-file case.
                            if last_pass_status_print is None or \
                                    (now - last_pass_status_print).total_seconds() >= args.status_interval:
                                if is_tty:
                                    write_status(status)
                                else:
                                    print(status)
                                last_pass_status_print = now
                except Exception as e:
                    if is_tty:
                        clear_line()
                    print(f"[{sat_cfg['name']}] ERROR during pass ({e!r}) - "
                          f"abandoning this pass rather than crashing the scheduler. "
                          f"Killing its flowgraph so it doesn't record silently forever.")
                    if notify_enabled:
                        notify("Satellite pass FAILED",
                               f"{sat_cfg['name']} - error during tracking: {e!r}")
                    log_pass_event("error", sat_cfg, active_pass, error=repr(e))
                    # an exception in the tracking code isn't something a
                    # blind relaunch fixes - treat the pass as finished so it
                    # can't loop. (active_pass can already be None here if the
                    # exception fired after the pass was reset.)
                    if active_pass is not None:
                        finished_passes.add(pass_key(active_pass))
                    try:
                        active_proc.terminate()
                        active_proc.wait(timeout=10)
                    except Exception:
                        pass
                    active_pass, active_proc = None, None
                    if not args.no_preposition:
                        preposition_for_next_pass(rot, passes, tles, sat_cfgs, ts, observer, now)

            if active_pass is None:
                for p in passes:
                    if p["aos_dt"] <= now < p["los_dt"]:
                        key = pass_key(p)
                        if not launch_allowed(key, now, launch_attempts, finished_passes):
                            continue  # already ended, given up on, or waiting to retry
                        if is_tty:
                            clear_line()
                        sat_cfg = sat_cfgs[p["norad"]]
                        attempt = note_launch(key, now, launch_attempts)
                        print(f"[{sat_cfg['name']}] AOS - launching {sat_cfg['script']}"
                              + (f" (attempt {attempt}/{MAX_LAUNCH_ATTEMPTS})"
                                 if attempt > 1 else ""))
                        if notify_enabled and attempt == 1:
                            notify("Satellite pass starting", f"{sat_cfg['name']} - AOS")
                        cmd, record_iq_value = build_launch_command(
                            sat_cfg, p, args.record_iq, iq_capable, record_iq_ok)
                        active_proc = subprocess.Popen(cmd)
                        active_pass = p
                        log_pass_event("started", sat_cfg, p,
                                       record_iq=record_iq_value, attempt=attempt)
                        time.sleep(3)  # let the flowgraph come up before polling rigctld
                        break

            if active_pass is None:
                upcoming = [p for p in passes if p["aos_dt"] > now]
                if upcoming:
                    nxt = upcoming[0]
                    sat_cfg = sat_cfgs[nxt["norad"]]
                    status = (f"Next pass: {sat_cfg['name']} in "
                              f"{format_countdown(nxt['aos_dt'] - now)} "
                              f"(AOS {nxt['aos']}, max el {nxt.get('max_elevation_deg', '?')} deg)")
                else:
                    status = "No further approved passes in schedule.yaml."

                if is_tty:
                    write_status(status)
                elif last_idle_status_print is None or \
                        (now - last_idle_status_print).total_seconds() >= args.status_interval:
                    print(status)
                    last_idle_status_print = now

            time.sleep(1)
    except KeyboardInterrupt:
        if is_tty:
            clear_line()
        if active_proc is not None:
            active_proc.terminate()
            try:
                active_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                active_proc.kill()
        rig.stop()
        print("Stopped.")


if __name__ == "__main__":
    main()
