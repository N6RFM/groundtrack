#!/usr/bin/env python3
"""
Adds a new satellite's config entry to satellites.yaml.

Only ever touches satellites.yaml - never generates or modifies a .grc.
Automated .grc generation was removed after repeatedly proving fragile
in ways GRC's own editor doesn't have (a stray leftover block here, a
wrong internal id there, across three separate satellites). Building
flowgraphs/<name>.grc - by hand, or by copying and adapting an existing
one in GRC - is a deliberate manual step, same as everything .grc-shaped
in this toolkit now.

Usage:
    python3 add_satellite.py --name GEOSCAN-3 --norad 64881 --freq 435530000
    python3 add_satellite.py --name SCIONX --norad 69880 --freq 437500000 --record-only
    python3 add_satellite.py --name BY70-4 --norad 98247 --freq 437443000 \\
        --record-only --disabled

After running this you still need to:
  1. Build flowgraphs/<name>.grc yourself in GRC - copy an existing
     satellite's .grc as a starting point if that's easier
  2. grcc flowgraphs/<name>.grc
  3. If it needs extra_outputs (a direct-connection consumer bypassing
     relay.py), add those with edit_satellite.py or the GUI's Edit dialog
  4. python3 preflight.py to confirm everything lines up
"""

import argparse
import yaml

CONFIG_PATH = "satellites.yaml"


def slugify(name):
    return name.lower().replace("-", "").replace(" ", "")


def next_free_ports(cfg):
    used_producer = {s["producer_port"] for s in cfg.get("satellites", [])
                      if "producer_port" in s}
    used_consumer = {s["consumer_port"] for s in cfg.get("satellites", [])
                      if "consumer_port" in s}
    prod = 9101
    while prod in used_producer:
        prod += 1
    cons = 8101
    while cons in used_consumer:
        cons += 1
    return prod, cons


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="e.g. GEOSCAN-3")
    ap.add_argument("--norad", required=True, type=int)
    ap.add_argument("--freq", required=True, type=int, help="downlink frequency in Hz")
    ap.add_argument("--min-elev", type=float, default=15.0)
    ap.add_argument("--record-only", action="store_true",
                     help="no producer_port/consumer_port - no relay involvement, for a "
                          "satellite with no decoder yet, or one whose outputs connect "
                          "directly via extra_outputs (add those separately afterward)")
    ap.add_argument("--producer-port", type=int, default=None,
                     help="override the auto-assigned producer port (decode-and-relay only)")
    ap.add_argument("--consumer-port", type=int, default=None,
                     help="override the auto-assigned consumer port (decode-and-relay only)")
    ap.add_argument("--disabled", action="store_true",
                     help="add with enabled: false, so it's configured but not yet scheduled "
                          "(toggle on later with toggle_satellite.py --enable)")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    dupes = [s["name"] for s in cfg.get("satellites", []) if s["norad"] == args.norad]
    if dupes:
        print(f"NOTE: NORAD {args.norad} is already configured as {', '.join(dupes)} - "
              f"adding another satellite with the same NORAD is fine for comparing "
              f"different frequencies/decode approaches against the same physical "
              f"object, but since they share one orbit, every pass will have "
              f"identical AOS/LOS times for all of them. Only enable one of these "
              f"at a time - a single SDR can't run two flowgraphs during the same "
              f"pass window.")

    slug = slugify(args.name)
    grc_path = f"flowgraphs/{slug}.grc"

    producer_port, consumer_port = (None, None) if args.record_only else next_free_ports(cfg)
    if not args.record_only:
        if args.producer_port is not None:
            used = {s["producer_port"] for s in cfg.get("satellites", []) if "producer_port" in s}
            if args.producer_port in used:
                raise SystemExit(f"--producer-port {args.producer_port} is already used by "
                                  f"another satellite - pick a different one")
            producer_port = args.producer_port
        if args.consumer_port is not None:
            used = {s["consumer_port"] for s in cfg.get("satellites", []) if "consumer_port" in s}
            if args.consumer_port in used:
                raise SystemExit(f"--consumer-port {args.consumer_port} is already used by "
                                  f"another satellite - pick a different one")
            consumer_port = args.consumer_port

    new_entry = {
        "name": args.name,
        "norad": args.norad,
        "freq_hz": args.freq,
        "script": f"flowgraphs/{slug}.py",
        "min_elev_deg": args.min_elev,
    }
    if not args.record_only:
        new_entry["producer_port"] = producer_port
        new_entry["consumer_port"] = consumer_port
    if args.disabled:
        new_entry["enabled"] = False
    cfg.setdefault("satellites", []).append(new_entry)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    if args.record_only:
        print(f"Added {args.name} to {CONFIG_PATH} as recording-only "
              f"(no producer_port/consumer_port - no relay involvement)")
    else:
        print(f"Added {args.name} to {CONFIG_PATH}: "
              f"producer_port={producer_port}, consumer_port={consumer_port}")
    if args.disabled:
        print(f"Added as disabled - enable later with: "
              f"python3 toggle_satellite.py --enable {args.name}")

    print(f"\nStill needed:")
    print(f"  1. Build {grc_path} yourself in GRC - an existing satellite's .grc "
          f"is a reasonable starting point to copy and adapt")
    print(f"  2. grcc {grc_path}")
    if not args.record_only:
        print(f"  3. Point its decoder file at a real *.yml (or clear it to rely on "
              f"norad auto-lookup), and its network_socket_pdu at port {producer_port}, "
              f"type TCP_CLIENT")
    print(f"  {'4' if not args.record_only else '3'}. If it needs extra_outputs, run "
          f"python3 suggest_extra_outputs.py {args.name} - it scans the .grc you just "
          f"built and generates the edit_satellite.py commands directly from the real "
          f"block ports/addresses, so nothing needs to be typed by hand")
    print(f"  {'5' if not args.record_only else '4'}. python3 preflight.py")


if __name__ == "__main__":
    main()
