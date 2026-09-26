#!/usr/bin/env python3
"""
Edit an existing satellite's fields in satellites.yaml.

Only ever touches satellites.yaml - never the .grc. Automated .grc
editing has repeatedly proven fragile (a stray leftover block here, a
wrong internal id there) in ways GRC's own editor doesn't have. If a
change here (a new frequency, a different NORAD) needs the .grc updated
to match, that's a deliberate manual step in GRC itself - this script
tells you when that's the case rather than trying to do it for you.

Only touches fields you actually pass. Leaves everything else alone.

Usage:
    python3 edit_satellite.py NAME --norad 12345
    python3 edit_satellite.py NAME --freq 437443000
    python3 edit_satellite.py NAME --min-elev 20
    python3 edit_satellite.py NAME --producer-port 9107 --consumer-port 8107
    python3 edit_satellite.py NAME --enabled
    python3 edit_satellite.py NAME --record-iq-toggle
        # marks that NAME's .grc has a record_iq Parameter block wired up,
        # so run_passes.py's --record-iq flag can actually control it
    python3 edit_satellite.py NAME --disabled

    # extra_outputs - a satellite with a second live output that a
    # specific downstream app connects to directly, bypassing relay.py
    # (see check_extra_outputs() in preflight.py for the full rationale).
    # Adding with a name that already exists on this satellite replaces
    # that entry rather than duplicating it, so re-running the same
    # command with a corrected value is safe. This still only ever
    # touches satellites.yaml - if the .grc's block itself needs to
    # change (its name, port, or address), that's a separate edit in
    # GRC, same as everything else here.
    python3 edit_satellite.py NAME --extra-output-name ssdv_viewer \\
        --extra-output-protocol tcp_server \\
        --extra-output-block network_socket_pdu_0 --extra-output-port 9985

    python3 edit_satellite.py NAME --extra-output-name ssdv_viewer \\
        --extra-output-protocol tcp_bridge \\
        --extra-output-block network_socket_pdu_0 \\
        --extra-output-port 9985 --extra-output-bridge-port 19985

    python3 edit_satellite.py NAME --remove-extra-output ssdv_viewer
"""

import argparse
import sys
import yaml

CONFIG_PATH = "satellites.yaml"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="satellite name to edit, exactly as it appears in satellites.yaml")
    ap.add_argument("--norad", type=int, default=None)
    ap.add_argument("--freq", type=int, default=None, help="downlink frequency in Hz")
    ap.add_argument("--min-elev", type=float, default=None)
    ap.add_argument("--producer-port", type=int, default=None)
    ap.add_argument("--consumer-port", type=int, default=None)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--enabled", action="store_true")
    group.add_argument("--disabled", action="store_true")
    group2 = ap.add_mutually_exclusive_group()
    group2.add_argument("--record-iq-toggle", action="store_true",
                         help="mark that this satellite's .grc has a record_iq "
                              "Parameter block wired to its Advanced File Sink's "
                              "Record on Start field - lets run_passes.py's "
                              "--record-iq flag actually control this satellite")
    group2.add_argument("--no-record-iq-toggle", action="store_true",
                         help="this satellite's .grc has no such toggle - "
                              "run_passes.py's --record-iq flag won't be passed to it")

    ap.add_argument("--extra-output-name", default=None,
                     help="add/replace an extra_outputs entry with this name")
    ap.add_argument("--extra-output-protocol",
                     choices=["tcp_server", "tcp_client", "tcp_bridge"],
                     default=None)
    ap.add_argument("--extra-output-block", default=None,
                     help="the block's exact name in the .grc, e.g. network_socket_pdu_0")
    ap.add_argument("--extra-output-port", type=int, default=None,
                     help="the flowgraph's own port")
    ap.add_argument("--extra-output-bridge-port", type=int, default=None,
                     help="for tcp_bridge only - what the real downstream consumer "
                          "connects to (tcp_bridge.py sits between it and --extra-output-port)")
    ap.add_argument("--remove-extra-output", default=None, metavar="NAME",
                     help="remove the named extra_outputs entry")

    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    sats = cfg.get("satellites", [])

    sat = next((s for s in sats if s["name"] == args.name), None)
    if sat is None:
        names = ", ".join(s["name"] for s in sats)
        sys.exit(f"No satellite named {args.name!r} in {CONFIG_PATH}. Configured: {names}")

    changes = []
    grc_note_needed = False

    if args.norad is not None and args.norad != sat.get("norad"):
        if any(s is not sat and s.get("norad") == args.norad for s in sats):
            sys.exit(f"NORAD {args.norad} is already used by another satellite - refusing")
        changes.append(f"norad: {sat.get('norad')} -> {args.norad}")
        sat["norad"] = args.norad
        grc_note_needed = True

    if args.freq is not None and args.freq != sat.get("freq_hz"):
        changes.append(f"freq_hz: {sat.get('freq_hz')} -> {args.freq}")
        sat["freq_hz"] = args.freq
        grc_note_needed = True

    if args.min_elev is not None and args.min_elev != sat.get("min_elev_deg"):
        changes.append(f"min_elev_deg: {sat.get('min_elev_deg')} -> {args.min_elev}")
        sat["min_elev_deg"] = args.min_elev

    for port_field, arg_val in (("producer_port", args.producer_port),
                                 ("consumer_port", args.consumer_port)):
        if arg_val is not None and arg_val != sat.get(port_field):
            if any(s is not sat and s.get(port_field) == arg_val for s in sats):
                sys.exit(f"{port_field} {arg_val} is already used by another satellite - refusing")
            changes.append(f"{port_field}: {sat.get(port_field)} -> {arg_val}")
            sat[port_field] = arg_val
            grc_note_needed = True

    if args.enabled:
        if sat.get("enabled", True) is not True:
            changes.append("enabled: false -> true")
        sat["enabled"] = True
    elif args.disabled:
        if sat.get("enabled", True) is not False:
            changes.append("enabled: true -> false")
        sat["enabled"] = False

    if args.record_iq_toggle:
        if sat.get("record_iq_toggle", False) is not True:
            changes.append("record_iq_toggle: false -> true")
        sat["record_iq_toggle"] = True
    elif args.no_record_iq_toggle:
        if sat.get("record_iq_toggle", False) is not False:
            changes.append("record_iq_toggle: true -> false")
        sat["record_iq_toggle"] = False

    if args.remove_extra_output:
        existing = sat.get("extra_outputs", [])
        before = len(existing)
        sat["extra_outputs"] = [e for e in existing if e.get("name") != args.remove_extra_output]
        after = len(sat["extra_outputs"])
        if after == before:
            sys.exit(f"No extra_output named {args.remove_extra_output!r} on {args.name}")
        changes.append(f"removed extra_output '{args.remove_extra_output}'")
        if not sat["extra_outputs"]:
            del sat["extra_outputs"]

    if args.extra_output_name:
        if not args.extra_output_protocol or not args.extra_output_block:
            sys.exit("--extra-output-name needs --extra-output-protocol and "
                      "--extra-output-block too")
        if args.extra_output_port is None:
            sys.exit(f"protocol {args.extra_output_protocol} needs --extra-output-port")
        if args.extra_output_protocol == "tcp_bridge" and args.extra_output_bridge_port is None:
            sys.exit("protocol tcp_bridge also needs --extra-output-bridge-port - "
                      "that's what the real downstream consumer connects to")

        entry = {
            "name": args.extra_output_name,
            "protocol": args.extra_output_protocol,
            "block": args.extra_output_block,
            "port": args.extra_output_port,
        }
        if args.extra_output_protocol == "tcp_bridge":
            entry["bridge_port"] = args.extra_output_bridge_port

        existing = sat.setdefault("extra_outputs", [])
        replaced = False
        for i, e in enumerate(existing):
            if e.get("name") == args.extra_output_name:
                existing[i] = entry
                replaced = True
                break
        if not replaced:
            existing.append(entry)
        changes.append(f"{'replaced' if replaced else 'added'} extra_output "
                        f"'{args.extra_output_name}'")

    if not changes:
        print(f"No changes given for {args.name} - nothing to do.")
        return

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    print(f"Updated {args.name} in {CONFIG_PATH}:")
    for c in changes:
        print(f"  {c}")

    if grc_note_needed:
        print(f"\nNote: this only updates satellites.yaml, not the .grc. If the norad, "
              f"frequency, or ports need to match in flowgraphs/*.grc too, open it in GRC "
              f"and update those blocks by hand.")

    if args.extra_output_name or args.remove_extra_output:
        print(f"\nNote: this only updates satellites.yaml. If the .grc's actual block "
              f"(name, port, or address) needs to change too, edit that separately in GRC - "
              f"this never touches .grc content.")


if __name__ == "__main__":
    main()
