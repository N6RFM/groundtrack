#!/usr/bin/env python3
"""
Scans a satellite's real .grc for network_socket_pdu blocks not yet
claimed by an extra_outputs entry, and drives edit_satellite.py to add
them - using the block's ACTUAL name and port straight from the .grc,
never hand-typed or guessed.

zeromq_pub_msg_sink blocks are deliberately NOT candidates here. ZMQ
PUB/SUB is fire-and-forget - the flowgraph publishes without knowing or
caring whether anything's listening, so there's no listener-side
infrastructure to configure and nothing in this toolkit ever reads a
zeromq_pub address at runtime. Tracking it in satellites.yaml would be
pure documentation with no functional payoff, so it isn't tracked.
network_socket_pdu is different: a real TCP_SERVER a downstream app
needs to connect to, which is exactly what tcp_bridge.py's persistent
listener exists to serve.

This exists because every extra_outputs bug found tonight - a typo'd
port, a block name that didn't match, two entries with the same
name - came from transcribing values by hand. This tool removes the
transcription step entirely: the only things it ever asks for are a
name (which the .grc has no way to know) and, for tcp_bridge, a
bridge_port (a real decision, not a fact to look up).

Usage:
    python3 suggest_extra_outputs.py ASRTU-1_HYBRID
        # scans, reports what's already configured, and offers to add
        # anything unclaimed

    python3 suggest_extra_outputs.py ASRTU-1_HYBRID --dry-run
        # same scan, but only prints the edit_satellite.py commands it
        # would run - nothing is actually changed
"""

import argparse
import subprocess
import sys
import yaml

CONFIG_PATH = "satellites.yaml"
CANDIDATE_BLOCK_ID = "network_socket_pdu"


def load_cfg():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def find_satellite(cfg, name):
    for sat in cfg.get("satellites", []):
        if sat.get("name") == name:
            return sat
    sys.exit(f"No satellite named {name!r} in {CONFIG_PATH}.")


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="satellite name, exactly as in satellites.yaml")
    ap.add_argument("--dry-run", action="store_true",
                     help="print the edit_satellite.py commands, don't run them")
    args = ap.parse_args()

    cfg = load_cfg()
    sat = find_satellite(cfg, args.name)
    grc_path = sat.get("script", "").replace(".py", ".grc")
    if not grc_path:
        sys.exit(f"{args.name} has no script: set in satellites.yaml.")

    try:
        with open(grc_path) as f:
            grc = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"{grc_path} doesn't exist - build it in GRC first.")

    blocks = grc.get("blocks", [])
    claimed_block_names = {e.get("block") for e in sat.get("extra_outputs", [])}
    # the primary relay connection (if any) isn't an "extra" - it's already
    # tracked via producer_port, not extra_outputs
    primary_port = sat.get("producer_port")

    candidates = []
    for b in blocks:
        if b["id"] != CANDIDATE_BLOCK_ID:
            continue
        if b["name"] in claimed_block_names:
            continue
        value = b["parameters"].get("port")
        if primary_port is not None:
            try:
                if int(value) == int(primary_port):
                    continue  # this is the primary relay connection, not an extra
            except (TypeError, ValueError):
                pass
        candidates.append((b["name"], value, b["parameters"].get("type")))

    if not candidates:
        print(f"{args.name}: every network-facing block in {grc_path} is "
              f"already accounted for (either claimed by an existing "
              f"extra_output, or the primary relay connection). Nothing to do.")
        return

    print(f"{args.name}: found {len(candidates)} unclaimed block(s) in {grc_path}:\n")
    for block_name, port, sock_type in candidates:
        label = f"port={port}" + (f" type={sock_type}" if sock_type else "")
        print(f"  {block_name}  ({label})")
    print()

    for block_name, port, sock_type in candidates:
        label = f"port={port}" + (f" type={sock_type}" if sock_type else "")
        print(f"--- {block_name}  ({label}) ---")
        out_name = input(f"  Name for this extra_output (blank to skip): ").strip()
        if not out_name:
            print("  skipped.\n")
            continue

        import station
        cmd = [sys.executable, station.script_path("edit_satellite.py"), args.name,
               "--extra-output-name", out_name,
               "--extra-output-block", block_name]

        use_bridge = input("  Use tcp_bridge (a persistent downstream "
                            "listener across passes)? [Y/n]: ").strip().lower()
        if use_bridge in ("", "y", "yes"):
            bridge_port = input("  bridge_port for the real downstream "
                                 "consumer to connect to: ").strip()
            if not bridge_port.isdigit():
                print("  invalid bridge_port - skipped.\n")
                continue
            cmd += ["--extra-output-protocol", "tcp_bridge",
                    "--extra-output-port", str(port),
                    "--extra-output-bridge-port", bridge_port]
        else:
            cmd += ["--extra-output-protocol", sock_type.lower()
                    if sock_type else "tcp_server",
                    "--extra-output-port", str(port)]

        print("  " + " ".join(cmd))
        if args.dry_run:
            print("  (--dry-run: not actually running this)\n")
            continue
        subprocess.run(cmd)
        print()


if __name__ == "__main__":
    main()
