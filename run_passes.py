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
from datetime import datetime, timedelta, timezone
from skyfield.api import load, wgs84, EarthSatellite

CONFIG_PATH = "satellites.yaml"
SCHEDULE_PATH = "schedule.yaml"
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


def load_tles(path, wanted_norads):
    sats = {}
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    ts = load.timescale()
    for i in range(0, len(lines), 3):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        sat = EarthSatellite(l1, l2, name, ts)
        if sat.model.satnum in wanted_norads:
            sats[sat.model.satnum] = sat
    return sats


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
    satellite drifted more than threshold_deg from wherever the rotor was
    last commanded? If not, do nothing at all - no time-based forced send
    exists here, matching the reference implementation exactly. If so,
    compute a lead-ahead target and send that instead of cur_az/cur_el."""
    if rot is None:
        return
    if rot.last is not None:
        daz = abs(cur_az - rot.last[0])
        delv = abs(cur_el - rot.last[1])
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
    tles = load_tles(cfg["tle_file"], set(sat_cfgs))

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
                        print(f"[{sat_cfg['name']}] flowgraph exited early "
                              f"(code {active_proc.returncode}) - check its output above. "
                              f"Abandoning this pass; rotor/Doppler stopped for it.")
                        active_pass, active_proc = None, None
                        if not args.no_preposition:
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
                        active_proc.terminate()
                        try:
                            active_proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            active_proc.kill()
                        active_pass, active_proc = None, None
                        if not args.no_preposition:
                            preposition_for_next_pass(rot, passes, tles, sat_cfgs, ts, observer, now)
                    else:
                        dop = doppler_hz(sat, observer, t, sat_cfg["freq_hz"])
                        corrected = sat_cfg["freq_hz"] + dop
                        rig.set_freq(corrected)
                        maybe_update_rotor(rot, sat, observer, ts, now,
                                            az_deg, el_deg, active_pass["los_dt"])
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
                        if is_tty:
                            clear_line()
                        sat_cfg = sat_cfgs[p["norad"]]
                        print(f"[{sat_cfg['name']}] AOS - launching {sat_cfg['script']}")
                        if notify_enabled:
                            notify("Satellite pass starting", f"{sat_cfg['name']} - AOS")
                        cmd = [sys.executable, "-u", sat_cfg["script"]]
                        if sat_cfg.get("record_iq_toggle", False):
                            # only satellites whose .grc actually has the
                            # record_iq Parameter block wired up get this flag -
                            # everything else launches exactly as before
                            cmd += ["--record-iq", "1" if args.record_iq == "yes" else "0"]
                        active_proc = subprocess.Popen(cmd)
                        active_pass = p
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
