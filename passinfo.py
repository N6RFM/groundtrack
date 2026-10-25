#!/usr/bin/env python3
"""
Plain-text descriptions of where a pass goes in the sky, from the details plan_passes.py records for each pass
in schedule.yaml (aos_az, max_az, los_az, direction). No satellite maths here, so any script can import it.

    ASC  SE 135 > E 82 > NE 40     rises in the south-east, peaks in the east, sets in the north-east,
                                   satellite heading north (ascending)
"""

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(az_deg):
    """16-point compass name for an azimuth in degrees (0 = north, 90 = east)."""
    if az_deg is None:
        return "?"
    return _COMPASS[int((az_deg % 360) / 22.5 + 0.5) % 16]


def _point(az):
    return "?" if az is None else f"{compass(az)} {az:.0f}"


def direction_tag(p):
    """'ASC' (heading north), 'DESC' (heading south) or '' when the pass has no recorded details."""
    return {"ascending": "ASC", "descending": "DESC"}.get(str(p.get("direction") or ""), "")


def track_text(p):
    """'ASC  SE 135 > E 82 > NE 40' for a schedule entry, or '' when it has none of the details (an older schedule)."""
    if p.get("aos_az") is None and p.get("max_az") is None and p.get("los_az") is None:
        return ""
    return f"{direction_tag(p):<4} {_point(p.get('aos_az'))} > {_point(p.get('max_az'))} > {_point(p.get('los_az'))}".strip()


def stamp(dt=None):
    """The moment as '2026-10-24 23:44:58 EDT (03:44:58Z)': this computer's clock first, then UTC."""
    from datetime import datetime, timezone
    dt = (dt or datetime.now(timezone.utc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} ({dt.astimezone(timezone.utc).strftime('%H:%M:%S')}Z)"
