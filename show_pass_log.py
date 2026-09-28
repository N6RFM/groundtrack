#!/usr/bin/env python3
"""
Human-readable summary of pass_log.jsonl - run_passes.py's persistent,
append-only record of what actually happened during each real pass
(started/completed/crashed/error), as opposed to schedule.yaml's record
of what was predicted/approved. Correlates each "started" event with
whatever outcome followed it (by satellite + AOS time), so one line per
actual pass, not one line per raw log event.

Usage:
    python3 show_pass_log.py
    python3 show_pass_log.py --last 20
    python3 show_pass_log.py --failures-only
"""

import argparse
import json
import sys
from collections import OrderedDict


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default="pass_log.jsonl")
    ap.add_argument("--last", type=int, default=None,
                     help="only show the most recent N passes")
    ap.add_argument("--failures-only", action="store_true",
                     help="only show crashed/error passes")
    args = ap.parse_args()

    try:
        with open(args.path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        print(f"'{args.path}' doesn't exist yet - it's created the first "
              f"time run_passes.py actually launches a pass.")
        return

    # correlate: one entry per (norad, aos), merging its "started" record
    # with whatever outcome event followed
    by_key = OrderedDict()
    for rec in lines:
        key = (rec["norad"], rec["aos"])
        entry = by_key.setdefault(key, {"satellite": rec["satellite"],
                                         "norad": rec["norad"], "aos": rec["aos"]})
        if rec["event"] == "started":
            entry["record_iq"] = rec.get("record_iq")
            entry["started_at"] = rec["timestamp"]
            entry["attempts"] = entry.get("attempts", 0) + 1
        else:
            entry["outcome"] = rec["event"]
            entry["ended_at"] = rec["timestamp"]
            entry["detail"] = rec.get("reason") or rec.get("error") or \
                (f"exit code {rec['exit_code']}" if "exit_code" in rec else "")

    entries = list(by_key.values())
    if args.failures_only:
        entries = [e for e in entries if e.get("outcome") in ("crashed", "error")]
    if args.last:
        entries = entries[-args.last:]

    if not entries:
        print("No matching pass log entries.")
        return

    print(f"{'AOS':<20}{'Satellite':<16}{'Outcome':<12}{'IQ':<6}{'Detail'}")
    print("-" * 90)
    for e in entries:
        outcome = e.get("outcome", "(no end recorded - crashed without exit, "
                                    "or run_passes.py itself was killed)")
        iq = "?" if e.get("record_iq") is None else ("Y" if e["record_iq"] else "N")
        aos_display = e["aos"][:19].replace("T", " ")  # trim to seconds, drop the T
        detail = e.get("detail", "")
        if e.get("attempts", 1) > 1:
            detail += f" ({e['attempts']} launch attempts)"
        print(f"{aos_display:<20}{e['satellite']:<16}{outcome:<12}{iq:<6}{detail}")

    total = len(entries)
    failed = sum(1 for e in entries if e.get("outcome") in ("crashed", "error"))
    missing_end = sum(1 for e in entries if "outcome" not in e)
    print(f"\n{total} pass(es) shown, {failed} failed"
          + (f", {missing_end} with no recorded end (check for a crash of "
             f"run_passes.py itself, not just the flowgraph)" if missing_end else ""))


if __name__ == "__main__":
    main()
