#!/usr/bin/env python3
"""
Send a few KISS frames through the relay to your downstream decoder,
without waiting for a real pass. Two modes:

  --replay <file.kss>   replay real frames captured during a past pass
                         (from satellites_kiss_file_sink_0's output file) -
                         these will actually decode meaningfully, since
                         they're genuine captured data.

  --synthetic <n>        send n garbage-payload frames, just to prove the
                          relay/decoder connection and KISS framing work.
                          Content is meaningless - your decoder will likely
                          flag CRC/parse errors on these, which is fine;
                          the point is confirming frames arrive at all.

In both cases this connects to the satellite's producer_port as if it
were the flowgraph itself (relay.py must already be running), so it
exercises the exact same path real passes use.

Usage:
    python3 send_test_frames.py --satellite GEOSCAN-2 --replay geoscan2.kss
    python3 send_test_frames.py --satellite GEOSCAN-2 --synthetic 5
"""

import argparse
import socket
import time
import yaml

CONFIG_PATH = "satellites.yaml"
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


def kiss_escape(payload):
    out = bytearray()
    for b in payload:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    return bytes(out)


def kiss_frame(payload, port=0):
    return bytes([FEND, port << 4]) + kiss_escape(payload) + bytes([FEND])


def encode_callsign(call, ssid=0, last=False):
    call = call.upper().ljust(6)[:6]
    addr = bytes((ord(c) << 1) for c in call)
    ssid_byte = 0x60 | (ssid << 1) | (1 if last else 0)
    return addr + bytes([ssid_byte])


def synthetic_ax25_frame(seq):
    dest = encode_callsign("CQ")
    src = encode_callsign("N0TEST", ssid=1, last=True)
    control = bytes([0x03])   # UI frame
    pid = bytes([0xF0])       # no layer 3
    payload = f"TEST FRAME #{seq} - synthetic, not real telemetry".encode()
    return dest + src + control + pid + payload


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def producer_port_for(cfg, sat_name):
    for sat in cfg["satellites"]:
        if sat["name"].lower() == sat_name.lower():
            return sat["producer_port"]
    raise SystemExit(f"No satellite named {sat_name!r} in satellites.yaml")


def split_kiss_stream(raw):
    """Best-effort split of a raw captured KISS byte stream into individual
    frames. Doesn't handle escaped FEND bytes inside frame data specially -
    fine for replaying real captures, which rarely contain them."""
    parts = raw.split(bytes([FEND]))
    frames = []
    for p in parts:
        if p:  # skip empty segments between consecutive FENDs
            frames.append(bytes([FEND]) + p + bytes([FEND]))
    return frames


def send_frames(port, frames, delay):
    print(f"Connecting to relay producer port 127.0.0.1:{port} ...")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        print(f"Connected. Sending {len(frames)} frame(s), {delay}s apart.")
        for i, frame in enumerate(frames, 1):
            sock.sendall(frame)
            print(f"  sent frame {i}/{len(frames)} ({len(frame)} bytes)")
            time.sleep(delay)
    print("Done. Check your decoder's history pane for new entries.")


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--satellite", required=True, help="e.g. GEOSCAN-2")
    ap.add_argument("--replay", help="path to a .kss file with real captured frames")
    ap.add_argument("--synthetic", type=int, help="send N garbage-payload test frames")
    ap.add_argument("--count", type=int, default=5,
                     help="with --replay, how many frames to send (default 5, from the start of the file)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between frames")
    args = ap.parse_args()

    if not args.replay and not args.synthetic:
        raise SystemExit("Specify either --replay <file.kss> or --synthetic <n>")

    cfg = load_config()
    port = producer_port_for(cfg, args.satellite)

    if args.replay:
        with open(args.replay, "rb") as f:
            raw = f.read()
        frames = split_kiss_stream(raw)[:args.count]
        if not frames:
            raise SystemExit(f"No frames found in {args.replay} - is it a valid KISS capture?")
        print(f"Loaded {len(frames)} real frame(s) from {args.replay}")
    else:
        frames = [kiss_frame(synthetic_ax25_frame(i)) for i in range(1, args.synthetic + 1)]
        print(f"Generated {len(frames)} synthetic test frame(s) "
              f"(garbage payload - won't decode meaningfully)")

    send_frames(port, frames, args.delay)


if __name__ == "__main__":
    main()
