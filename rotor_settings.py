"""
How often and how far the antenna rotor is told to move, read from and written to satellites.yaml's top level.

    rot_threshold_deg: 5.0        # both axes: only move once the satellite has drifted this far (default 5)
    rot_threshold_az_deg: 8.0     # optional: azimuth on its own (overrides rot_threshold_deg)
    rot_threshold_el_deg: 3.0     # optional: elevation on its own
    rot_min_interval_s: 5         # optional: never send a new command sooner than this after the last (default 0 = no limit)

A command is sent only when BOTH hold: the satellite has drifted at least the threshold from where the rotor is
now, and at least rot_min_interval_s seconds have passed since the last tracking command. The tools in this folder
write these keys with set_rotor.py or the GUI's Rotor settings window; edits keep every other line and comment.
"""
import os
import re

import yaml

DEFAULT_THRESHOLD_DEG = 5.0
MAX_STEP_DEG = 45.0     # beyond this the lead-ahead search (which assumes the satellite keeps moving away) stops being reliable
KEYS = ("rot_threshold_deg", "rot_threshold_az_deg", "rot_threshold_el_deg", "rot_min_interval_s")


def _num(cfg, key):
    v = cfg.get(key)
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def effective(cfg, threshold_deg=None, min_interval_s=None):
    """-> {'az': deg, 'el': deg, 'interval': seconds}: what run_passes uses. Command-line values (threshold_deg,
    min_interval_s) beat the file; a per-axis key beats rot_threshold_deg; anything unusable falls back to the default."""
    both = threshold_deg if threshold_deg is not None else _num(cfg, "rot_threshold_deg")
    if both is None or both <= 0:
        both = DEFAULT_THRESHOLD_DEG

    def axis(key):
        if threshold_deg is not None:        # a command-line value covers both axes
            return both
        v = _num(cfg, key)
        return v if v is not None and v > 0 else both

    interval = min_interval_s if min_interval_s is not None else _num(cfg, "rot_min_interval_s")
    if interval is None or interval < 0:
        interval = 0.0
    return {"az": axis("rot_threshold_az_deg"), "el": axis("rot_threshold_el_deg"), "interval": float(interval)}


def problem(az, el, interval):
    """Why these values can't be used, or None."""
    for name, v, top in (("Azimuth step", az, MAX_STEP_DEG), ("Elevation step", el, MAX_STEP_DEG)):
        if v is None or not 0 < v <= top:
            return f"{name} must be a number above 0 and at most {top:g} degrees."
    if interval is None or not 0 <= interval <= 3600:
        return "The minimum time between commands must be 0 (no limit) to 3600 seconds."
    return None


def _fmt(v):
    v = float(v)
    return str(int(v)) if v == int(v) else f"{v:g}"


def set_keys(path, changes):
    """Set (value) or remove (None) top-level keys in the YAML file at `path`, changing nothing else: other lines,
    comments and ordering stay exactly as they are. New keys go just before `satellites:`. Checked by re-reading the
    result before it replaces the file; returns the new text."""
    with open(path, newline="") as f:
        lines = f.read().split("\n")
    for key, value in changes.items():
        pat = re.compile(r"^" + re.escape(key) + r":(?P<sp>[ \t]*)(?P<val>[^#\n]*?)(?P<rest>[ \t]*#.*)?$")
        hit = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
        if value is None:
            if hit is not None:
                del lines[hit]
            continue
        new = f"{key}: {_fmt(value)}"
        if hit is not None:
            m = pat.match(lines[hit])
            lines[hit] = new + (m.group("rest") or "")
        else:
            at = next((i for i, ln in enumerate(lines) if re.match(r"^satellites:", ln)), len(lines))
            while at > 0 and lines[at - 1].strip() == "":      # keep the blank line that separated the settings
                at -= 1
            lines.insert(at, new)
    text = "\n".join(lines)
    back = yaml.safe_load(text) or {}
    for key, value in changes.items():
        got = back.get(key)
        if (value is None and got is not None) or (value is not None and (got is None or float(got) != float(value))):
            raise ValueError(f"could not edit {key} safely - nothing was written")
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        f.write(text)
    os.replace(tmp, path)
    return text


def changes_for(az, el, interval):
    """The key edits that make `effective()` return these values: equal axes -> one rot_threshold_deg and no
    per-axis keys; different axes -> per-axis keys (rot_threshold_deg is left alone, they override it)."""
    ch = {}
    if abs(az - el) < 1e-9:
        ch.update({"rot_threshold_deg": az, "rot_threshold_az_deg": None, "rot_threshold_el_deg": None})
    else:
        ch.update({"rot_threshold_az_deg": az, "rot_threshold_el_deg": el})
    ch["rot_min_interval_s"] = interval if interval > 0 else None
    return ch
