#!/usr/bin/env python3
"""
Checks that run around every pass, so a pass that cannot record - or did not - is noticed at once
instead of after the satellite has gone.

Before a pass (run_passes.py calls these at start-up and again just before each launch):
    pre_pass_problems()   the flowgraph file exists, it will really record (the compiled script does not
                          have Record On Start frozen to False), the IQ folder is writable, there is room
                          on the disk for the whole pass, and the TLE is not stale

After a pass:
    verify_iq()           did an IQ file appear, and is it a sensible size for how long the pass ran?
                          OK / SHORT / MISSING, written to pass_log.jsonl as an "iq_check" event

Everything here is read-only apart from probing the IQ folder, and nothing raises: a check that cannot
be made says so (UNKNOWN) rather than stopping a pass.
"""

import ast
import glob
import os
import re
import shutil
import time

import yaml

# The sink's input is complex float32: 8 bytes per sample, whatever its bits_per_sample says.
BYTES_PER_SAMPLE = 8
SHORT_FRACTION = 0.10          # a file under 10% of the expected size counts as SHORT
MIN_FREE_BYTES = 500 * 1024 * 1024
STALE_TLE_DAYS = 14


def _param_float(value):
    try:
        return float(str(value).strip("'\""))
    except (TypeError, ValueError):
        return None


def grc_text_for(sat_cfg):
    path = str(sat_cfg.get("script") or "").replace(".py", ".grc")
    return path if path.endswith(".grc") else None


def sink_info(grc_path):
    """What the .grc's IQ file sink(s) are set to: {"basedir", "sample_rate", "record_on_start"} for the
    first AdvFileSink-style block, or None when there is none or the file can't be read. Values that are
    expressions rather than plain literals come back as None for that field."""
    try:
        with open(grc_path) as f:
            grc = yaml.safe_load(f)
        for b in grc.get("blocks", []):
            p = b.get("parameters") or {}
            if "basedir" in p and "recordOnStart" in p:
                basedir = str(p.get("basedir") or "")
                literal = bool(basedir) and not re.search(r"[(){}+]", basedir) and re.fullmatch(r"[\w./~ -]+", basedir)
                return {
                    "basedir": os.path.expanduser(basedir) if literal else None,
                    "basefile": str(p.get("basefile") or ""),
                    "sample_rate": _param_float(p.get("sampleRate")),
                    "record_on_start": str(p.get("recordOnStart")).strip("'\""),
                }
    except Exception:
        pass
    return None


def compiled_record_state(script, grc_path):
    """(frozen, wanted): `wanted` is the .grc's Record On Start expression; `frozen` is True when the
    compiled script passes a literal False/0 to the file sink for it. (False, None) when it can't tell."""
    try:
        with open(script) as f:
            tree = ast.parse(f.read())
        with open(grc_path) as f:
            grc = f.read()
    except (OSError, SyntaxError):
        return False, None
    m = re.search(r"^\s*recordOnStart:\s*(.+?)\s*$", grc, re.M)
    if not m:
        return False, None
    wanted = m.group(1).strip("'\"")
    if wanted in ("False", "0"):
        return False, wanted
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "AdvFileSink"
                and len(node.args) > 8):
            arg = node.args[8]
            return (isinstance(arg, ast.Constant) and arg.value in (False, 0)), wanted
    return False, wanted


def wants_record(sat_cfg, record_iq_value, sink):
    """Will this launch be expected to write IQ? record_iq_value is what run_passes hands the flowgraph
    (True/False), or None when it passes nothing and the .grc's own setting applies."""
    if record_iq_value is not None:
        return bool(record_iq_value)
    if sink is None:
        return False
    return sink["record_on_start"] not in ("False", "0", "")


def expected_bytes(sink, seconds):
    if not sink or not sink.get("sample_rate"):
        return None
    return int(sink["sample_rate"] * BYTES_PER_SAMPLE * max(seconds, 0))


def tle_age_days(sat):
    try:
        return (time.time() - sat.epoch.utc_datetime().timestamp()) / 86400.0
    except Exception:
        return None


def pre_pass_problems(sat_cfg, record_iq_value, pass_seconds, sat=None):
    """[(severity, message)] for one upcoming pass; severity is "block" (cannot run at all) or "warn".
    An empty list means nothing was found wrong."""
    out = []
    name = sat_cfg.get("name", "?")
    script = sat_cfg.get("script", "")
    if not os.path.exists(script):
        out.append(("block", f"{name}: {script} does not exist - compile it: grcc -o flowgraphs flowgraphs/<name>.grc"))
        return out
    grc = grc_text_for(sat_cfg)
    sink = sink_info(grc) if grc and os.path.exists(grc) else None
    if wants_record(sat_cfg, record_iq_value, sink):
        if sink is None:
            out.append(("warn", f"{name}: asked to record IQ, but {grc or 'its .grc'} has no IQ file sink to read"))
        else:
            frozen, wanted = compiled_record_state(script, grc)
            if frozen:
                out.append(("warn", f"{name}: {script} has Record On Start frozen to False (the .grc says {wanted}) - "
                                    f"this pass will NOT record. Fix: rm -rf ~/.cache/grc_gnuradio, then "
                                    f"grcc -o flowgraphs {grc}"))
            d = sink["basedir"]
            if d:
                try:
                    os.makedirs(d, exist_ok=True)
                    ok = os.access(d, os.W_OK)
                except OSError:
                    ok = False
                if not ok:
                    out.append(("warn", f"{name}: the IQ folder {d} cannot be written to - this pass will not be saved"))
                else:
                    free = shutil.disk_usage(d).free
                    need = max(MIN_FREE_BYTES, int((expected_bytes(sink, pass_seconds) or 0) * 1.2))
                    if free < need:
                        out.append(("warn", f"{name}: only {free / 1e9:.1f} GB free in {d}, a full pass needs about "
                                            f"{need / 1e9:.1f} GB - free some space"))
    if sat is not None:
        age = tle_age_days(sat)
        if age is not None and age > STALE_TLE_DAYS:
            out.append(("warn", f"{name}: its TLE is {age:.0f} days old - Doppler and pointing may be off. "
                                f"Run: python3 update_tle.py"))
    return out


def iq_files_since(basedir, since_ts):
    """IQ files in the folder written to since `since_ts` (any name: a mistyped basefile must not hide a recording)."""
    found = []
    for path in glob.glob(os.path.join(basedir, "*.iq")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime >= since_ts - 2:
            found.append((path, st.st_size))
    return sorted(found)


def verify_iq(sat_cfg, record_iq_value, started_ts, ended_ts):
    """{"status": OK|SHORT|MISSING|SKIPPED|UNKNOWN, "files": [...], "bytes": n, "expected": n|None, "note": str}"""
    grc = grc_text_for(sat_cfg)
    sink = sink_info(grc) if grc and os.path.exists(grc) else None
    if not wants_record(sat_cfg, record_iq_value, sink):
        return {"status": "SKIPPED", "files": [], "bytes": 0, "expected": None, "note": "IQ recording was off for this pass"}
    if sink is None or not sink["basedir"]:
        return {"status": "UNKNOWN", "files": [], "bytes": 0, "expected": None,
                "note": "could not tell where this flowgraph writes its IQ"}
    files = iq_files_since(sink["basedir"], started_ts)
    total = sum(s for _, s in files)
    exp = expected_bytes(sink, ended_ts - started_ts)
    if not files or total == 0:
        return {"status": "MISSING", "files": [p for p, _ in files], "bytes": total, "expected": exp,
                "note": f"no IQ file was written to {sink['basedir']}"}
    if exp and total < exp * SHORT_FRACTION:
        return {"status": "SHORT", "files": [p for p, _ in files], "bytes": total, "expected": exp,
                "note": f"{total / 1e6:.1f} MB written, about {exp / 1e6:.0f} MB expected"}
    return {"status": "OK", "files": [p for p, _ in files], "bytes": total, "expected": exp,
            "note": f"{total / 1e6:.1f} MB in {len(files)} file(s)"}
