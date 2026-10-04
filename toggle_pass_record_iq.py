#!/usr/bin/env python3
"""
Toggle IQ recording on or off for specific upcoming passes in the queue -
a per-pass-instance override, independent of which satellite it is.
Distinct from the two other record_iq mechanisms already in this
toolkit, and takes priority over both when set:

  1. Capability (per-satellite) - does this satellite's .grc even have the
     record_iq Parameter block wired up at all. Doesn't decide yes/no, just
     whether it CAN. Read from the .grc itself; nothing to declare in
     satellites.yaml (record_iq_toggle: false there opts a satellite out).
  2. --record-iq (run_passes.py, session-wide) - the default decision
     for every pass launched during one run_passes.py session.
  3. record_iq (schedule.yaml, per-pass) - THIS tool. Overrides #2 for
     one specific queued pass, regardless of satellite. If unset on a
     pass, run_passes.py falls back to the session-wide --record-iq.

Only ever writes to schedule.yaml - never touches satellites.yaml or
any .grc, same as every other queue-management tool here.

Usage:
    python3 toggle_pass_record_iq.py
        # interactive: lists upcoming approved passes, prompts for which
        # to toggle
"""

import sys
from datetime import datetime, timezone

import yaml

SCHEDULE_PATH = "schedule.yaml"
CONFIG_PATH = "satellites.yaml"


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    import station
    station.enter()
    try:
        with open(SCHEDULE_PATH) as f:
            schedule = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"{SCHEDULE_PATH} not found - run plan_passes.py first.")

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    from edit_satellite import record_iq_capable
    capable_norads = {s["norad"] for s in cfg.get("satellites", [])
                       if record_iq_capable(s)}

    now = datetime.now(timezone.utc)
    passes = schedule.get("passes", [])
    upcoming = [p for p in passes if p.get("approved")
                and parse_iso(p["los"]) > now]
    upcoming.sort(key=lambda p: p["aos"])

    if not upcoming:
        print("No upcoming approved passes in the queue.")
        return

    print(f"{'#':<4}{'Satellite':<16}{'AOS':<22}{'record_iq':<14}{'capable?'}")
    print("-" * 70)
    for i, p in enumerate(upcoming):
        capable = p["norad"] in capable_norads
        override = p.get("record_iq")
        status = ("(no override - uses session default)" if override is None
                  else "ON" if override else "OFF")
        print(f"{i:<4}{p['name']:<16}{p['aos']:<22}{status:<14}"
              f"{'yes' if capable else 'NO - toggle has no effect'}")

    print()
    selection = input("Pass number(s) to toggle (comma-separated, blank to "
                       "cancel): ").strip()
    if not selection:
        print("Cancelled - nothing changed.")
        return

    try:
        indices = [int(x.strip()) for x in selection.split(",")]
    except ValueError:
        sys.exit("Couldn't parse that as a list of numbers.")

    invalid = [i for i in indices if i < 0 or i >= len(upcoming)]
    if invalid:
        sys.exit(f"Pass number(s) out of range: {invalid}")

    incapable = [i for i in indices if upcoming[i]["norad"] not in capable_norads]
    if incapable:
        names = sorted({upcoming[i]["name"] for i in incapable})
        print(f"Skipping {names} - not toggle-capable, so a per-pass override would "
              f"have no effect. A satellite is capable once its .grc is wired for the "
              f"record_iq toggle - python3 wire_record_iq.py <name> does that in one "
              f"step, then ./regen_all.sh; nothing needs declaring in satellites.yaml "
              f"(unless record_iq_toggle: false is set there, which opts it out). "
              f"See docs/adding-satellites.md, 'Toggling IQ recording'.")
        indices = [i for i in indices if i not in incapable]
    if not indices:
        print("Nothing left to toggle.")
        return

    choice = input("Set to [1] record, [0] don't record, or [c] clear "
                    "override (fall back to session default)? ").strip().lower()
    if choice == "c":
        new_value = None
    elif choice in ("1", "0"):
        new_value = choice == "1"
    else:
        sys.exit("Not a recognized choice - nothing changed.")

    for i in indices:
        if new_value is None:
            upcoming[i].pop("record_iq", None)
        else:
            upcoming[i]["record_iq"] = new_value

    # write back into the ORIGINAL passes list, not just `upcoming` - past/
    # unapproved passes must be preserved untouched
    upcoming_by_id = {id(p): p for p in upcoming}
    for i, p in enumerate(passes):
        if id(p) in upcoming_by_id:
            passes[i] = upcoming_by_id[id(p)]

    with open(SCHEDULE_PATH, "w") as f:
        yaml.dump({"passes": passes}, f, sort_keys=False, default_flow_style=False)

    label = "cleared" if new_value is None else ("ON" if new_value else "OFF")
    print(f"Updated {len(indices)} pass(es): record_iq {label}.")


if __name__ == "__main__":
    main()
