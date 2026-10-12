#!/usr/bin/env python3
"""
Does a NORAD number belong to the satellite it is written next to?

A single mistyped digit (BEE-1012 entered as 98917 instead of 98197) gives a satellite that never
gets a TLE and nothing says why. SatNOGS' database knows which name goes with each number, so this
looks the number up and compares the name. Used when a satellite is added, and by preflight.py.

Never blocks anything: with no network, a slow SatNOGS or an unknown number the answer is simply
"couldn't tell". Answers are kept in .satnogs_names.json (not committed) for 30 days.
"""

import json
import os
import re
import time
import urllib.request

URL = "https://db.satnogs.org/api/satellites/?norad_cat_id={norad}&format=json"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".satnogs_names.json")
TTL_S = 30 * 86400
USER_AGENT = "groundtrack/1.0 (+https://github.com/N6RFM/groundtrack)"


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def _stem(s):
    m = re.match(r"[A-Z]+", str(s).strip().upper())
    return m.group(0) if m else ""


def names_match(mine, theirs):
    """Loose match: the same letters-and-digits, one containing the other, or the same leading word of 4+ letters
    (GEOSCAN-1 against 'Geoscan-Edelveis')."""
    a = _norm(mine)
    if not a:
        return True
    for t in theirs:
        b = _norm(t)
        if not b:
            continue
        if a == b or (len(b) >= 3 and b in a) or (len(a) >= 3 and a in b):
            return True
        x, y = _stem(mine), _stem(t)
        if len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x)):
            return True
    return False


def _load_cache():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(c):
    try:
        with open(CACHE, "w") as f:
            json.dump(c, f)
    except OSError:
        pass


def lookup(norad, use_cache=True, timeout=4):
    """{"found": bool, "names": [...], "follow": int|None, "status": str} from SatNOGS, or None when it can't be reached."""
    key = str(int(norad))
    cache = _load_cache() if use_cache else {}
    hit = cache.get(key)
    if hit and time.time() - hit.get("t", 0) < TTL_S:
        return hit["v"]
    try:
        req = urllib.request.Request(URL.format(norad=int(norad)), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    if data:
        d = data[0]
        names = [d.get("name", "")] + [n for n in re.split(r"[,\n]", d.get("names") or "") if n.strip()]
        v = {"found": True, "names": [n.strip() for n in names if n and n.strip()],
             "follow": d.get("norad_follow_id"), "status": d.get("status", "")}
    else:
        v = {"found": False, "names": [], "follow": None, "status": ""}
    if use_cache:
        cache[key] = {"t": time.time(), "v": v}
        _save_cache(cache)
    return v


def problem(name, norad, use_cache=True, timeout=4):
    """None when fine or unknowable; otherwise a one-sentence description of what looks wrong."""
    info = lookup(norad, use_cache=use_cache, timeout=timeout)
    if info is None:
        return None
    if not info["found"]:
        return (f"SatNOGS has no satellite numbered {norad}, so {name} will not get a TLE from it. "
                f"Check the number for a typo (search the name at https://db.satnogs.org); "
                f"a brand-new satellite can use tle/custom.txt instead.")
    if not names_match(name, info["names"]):
        return (f"SatNOGS says {norad} is {info['names'][0] if info['names'] else '?'}, not {name}. "
                f"Check the number for a typo.")
    return None
