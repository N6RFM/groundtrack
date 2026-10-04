"""
Receivers ("lanes") and paired passes - the pure logic, shared by run_passes.py,
plan_passes.py, pair_passes.py, show_queue.py, preflight.py and doctor.py so they can't
disagree about it.

A LANE is one Doppler channel: the rigctld port a flowgraph polls for its frequency.
One SDR, one lane. Two satellites on the same lane can never be recorded together (one
receiver). Two on different lanes can - but the beam only points one place, so that only
makes sense for satellites that are close together in the sky, and it is the user's
choice, made for one pair of passes at a time:

  - the schedule marks one pass `rides_with: "<norad>@<aos>"` of the other
  - the other is the LEADER: its TLE steers the rotor
  - each keeps its own Doppler - its own TLE, its own frequency - on its own lane
  - overlapping passes that are NOT paired behave exactly as they always have: the first
    to start records, the rest wait

A pass is either a leader, a companion or neither; there are no chains.
"""

import os

import yaml


def grc_doppler_port(grc_path):
    """The rigctld port the flowgraph's rig_freq_poller polls, read from the .grc itself:
    the flowgraph already says it, so nobody has to repeat it in a config file. None if
    there's no poller, or its port isn't a plain number."""
    with open(grc_path) as f:
        grc = yaml.safe_load(f)
    if not isinstance(grc, dict):
        return None          # an empty file, or not a flowgraph at all
    for block in grc.get("blocks") or []:
        if block.get("id") == "epy_block" and "rig_freq_poller" in str(block.get("name", "")):
            value = (block.get("parameters") or {}).get("rig_port")
            try:
                return int(str(value).strip().strip("'\""))
            except (TypeError, ValueError):
                return None
    return None


def doppler_port(sat, default, base_dir="."):
    """The lane a satellite's passes run on: an explicit `rig_port:` in its entry, else the
    port its .grc's poller polls, else the station's own rig_port (`default`). `base_dir`
    is the station's folder, for reading the .grc of a station you're not standing in."""
    explicit = sat.get("rig_port")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    script = str(sat.get("script") or "")
    if script.endswith(".py"):
        try:
            port = grc_doppler_port(os.path.join(base_dir, script[:-3] + ".grc"))
        except (OSError, yaml.YAMLError, AttributeError, TypeError):
            port = None
        if port:
            return port
    return default


def pass_ref(p):
    """How one pass is named inside another's `rides_with`: '<norad>@<aos>', e.g.
    '57172@2026-10-04T15:05:15Z'. Readable, and it survives hand-editing."""
    return f"{p['norad']}@{p['aos']}"


def overlap_s(a, b):
    """Seconds the two passes' windows overlap (0 if they don't). ISO strings, as written
    in schedule.yaml."""
    from datetime import datetime
    def t(s):
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return max(0.0, (min(t(a["los"]), t(b["los"])) - max(t(a["aos"]), t(b["aos"]))).total_seconds())


def leader_of(p, by_ref, lane_of):
    """The pass `p` rides with - or None if it doesn't, or the pairing can't hold: the leader
    isn't among the (approved) passes, is itself riding with someone, or is on the same lane
    (one SDR can't record two things at once, so a hand-edited pairing like that is ignored
    rather than obeyed)."""
    ref = p.get("rides_with")
    if not ref:
        return None
    lead = by_ref.get(ref)
    if lead is None or lead is p or lead.get("rides_with") or lane_of(lead) == lane_of(p):
        return None
    return lead


def pair_fault(p, by_ref, lane_of):
    """Why p's rides_with doesn't hold - so run_passes.py will ignore it - in plain language; None
    if p doesn't ride with anything, or the pairing is sound. For preflight."""
    ref = p.get("rides_with")
    if not ref:
        return None
    lead = by_ref.get(ref)
    if lead is None:
        return f"{ref} isn't an approved pass in this schedule"
    if lead is p:
        return "a pass can't ride with itself"
    if lead.get("rides_with"):
        return f"{lead['name']} at {lead['aos']} is itself riding with another pass (no chains)"
    if lane_of(lead) == lane_of(p):
        return (f"{p['name']} and {lead['name']} use the same receiver (Doppler channel {lane_of(p)}) - "
                f"one SDR can't record both")
    if overlap_s(lead, p) <= 0:
        return "the two passes don't overlap, so pairing them changes nothing"
    return None


def paired(a, b, by_ref, lane_of):
    return leader_of(a, by_ref, lane_of) is b or leader_of(b, by_ref, lane_of) is a


def may_run_alongside(p, running, by_ref, lane_of):
    """May `p` start while these passes are running? Only if the user paired it with every
    one of them. Nothing running: always."""
    return all(paired(p, r, by_ref, lane_of) for r in running)


def steering_pass(running, by_ref, lane_of):
    """The running pass the rotor follows: a leader whose companion is running too, else
    (nobody riding along) the one that started first. A companion only rides while its
    leader is running - on its own it steers like any pass, so the beam is never left
    unattended just because the pass it was paired with failed or ended."""
    riding = [r for r in running if any(leader_of(r, by_ref, lane_of) is x for x in running)]
    free = [r for r in running if not any(r is x for x in riding)]
    return (free or running)[0]


def candidate_pairs(passes, lane_of):
    """Every pair of approved passes that overlap in time on DIFFERENT lanes - the only
    ones that could be recorded together - earlier AOS first. Compares all pairs, not just
    neighbours: with two receivers, a short pass can sit inside a long one that a
    third pass also overlaps."""
    approved = sorted((p for p in passes if p.get("approved")), key=lambda p: p["aos"])
    out = []
    for i, a in enumerate(approved):
        for b in approved[i + 1:]:
            if b["aos"] >= a["los"]:
                continue
            if lane_of(a) != lane_of(b):
                out.append((a, b))
    return out


def pairing_problem(a, b, leader, passes, lane_of):
    """Why this pair can't be made (plain language), or None if it can. `leader` is a or b."""
    if leader is not a and leader is not b:
        return "the leader must be one of the two passes"
    if not (a.get("approved") and b.get("approved")):
        return "both passes must be approved"
    if lane_of(a) == lane_of(b):
        return (f"{a['name']} and {b['name']} use the same receiver (Doppler channel {lane_of(a)}) - "
                f"one SDR can't record two things at once")
    if overlap_s(a, b) <= 0:
        return "the passes don't overlap"
    companion = b if leader is a else a
    for p in (a, b):
        if p.get("rides_with") and p["rides_with"] != pass_ref(leader):
            return f"{p['name']} at {p['aos']} is already riding with another pass - unpair it first"
    for p in passes:
        if p.get("rides_with") in (pass_ref(a), pass_ref(b)) and p is not companion:
            return (f"{p['name']} at {p['aos']} is already paired with one of these - a pass is a "
                    f"leader or a companion, never both, and rides with one leader - unpair it first")
    return None


def set_pair(a, b, leader):
    """Record the pairing: the companion rides with the leader."""
    companion = b if leader is a else a
    companion["rides_with"] = pass_ref(leader)


def clear_pair(p, passes):
    """Unpair `p`, whichever side it is: drop its own rides_with, and the rides_with of any
    pass that rides with it. Returns how many passes were changed."""
    n = 0
    if p.pop("rides_with", None) is not None:
        n += 1
    ref = pass_ref(p)
    for q in passes:
        if q.get("rides_with") == ref:
            del q["rides_with"]
            n += 1
    return n
