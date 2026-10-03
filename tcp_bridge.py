#!/usr/bin/env python3
"""
tcp_bridge.py - the mirror-image of relay.py, for a satellite whose flowgraph
runs its own TCP_SERVER instead of connecting out as a TCP_CLIENT.

relay.py can only ever listen on both sides - every satellite that uses
it has a flowgraph that connects OUT to it as a client. Some flowgraphs
do the opposite: they run their own TCP_SERVER and wait for a specific
downstream app (an SSDV image viewer, say) to connect IN. That's fine
while the flowgraph is running, but the flowgraph is short-lived - it
only exists during a pass - so without something in between, the
downstream app has no stable endpoint to connect to between passes, and
has to detect the drop and reconnect at the start of every single one.

tcp_bridge.py solves that the same way relay.py solves it for the opposite
direction: it connects OUT to the flowgraph's TCP_SERVER as a client
(retrying patiently whenever the flowgraph isn't running - i.e. between
passes), while LISTENING persistently for the real downstream consumer.
The consumer gets one stable address to point at, for the life of a
session, exactly like a SatsDecoder tab does with relay.py.

Multiple satellites can share ONE bridge_port if they feed the same
downstream app (e.g. two satellites both feeding the same SSDV image
viewer) - each gets its own independent upstream connection, retrying on
its own, but they share a single real listening socket, so the
downstream app never has to change which port it's pointed at depending
on which satellite is actually passing. Since only one satellite's
flowgraph is ever actually running at a time (one shared SDR), only one
upstream connection is ever actually live in practice - the others just
sit retrying, harmlessly, until it's their turn.

One-directional only (flowgraph -> downstream consumer) - this is for a
data stream like SSDV frames, not a two-way control channel. Doesn't
parse or care what's inside the bytes, same as relay.py.

Configured per satellite via an extra_outputs entry with its own
dedicated protocol, tcp_bridge:

    extra_outputs:
      - name: ssdv_viewer
        protocol: tcp_bridge
        block: network_socket_pdu_0
        port: 9985          # THIS satellite's flowgraph TCP_SERVER -
                             # tcp_bridge.py connects out to it as a client
        bridge_port: 19985   # what the real downstream consumer (the
                             # SSDV Viewer app) should actually connect to -
                             # shared across every satellite that uses it

Disabled satellites (enabled: false) are skipped entirely, same as
relay.py.

Usage:
    python3 tcp_bridge.py [--verbose]
"""

import argparse
import asyncio
import sys
import yaml

CONFIG_PATH = "satellites.yaml"
RETRY_DELAY_S = 2
HEARTBEAT_EVERY = 30  # ~1 minute of quiet retries between "still waiting" logs


class DownstreamListener:
    """One shared listener per distinct bridge_port. Multiple satellites'
    upstream connections can all feed the same set of connected downstream
    consumers - that's what lets one stable endpoint work regardless of
    which of several satellites sharing a downstream app is actually
    passing right now."""

    def __init__(self, bridge_port, verbose=False):
        self.bridge_port = bridge_port
        self.verbose = verbose
        self.downstream_writers = []
        self.label = f"bridge:{bridge_port}"

    def log(self, msg):
        print(f"[{self.label}] {msg}", flush=True)

    def vlog(self, msg):
        if self.verbose:
            self.log(msg)

    async def handle_downstream(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self.log(f"downstream consumer connected: {peer}")
        self.downstream_writers.append(writer)
        try:
            while not reader.at_eof():
                await reader.read(65536)  # consumer never sends us anything meaningful
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.log(f"downstream consumer disconnected: {peer}")
            if writer in self.downstream_writers:
                self.downstream_writers.remove(writer)
            writer.close()

    async def broadcast(self, data):
        dead = []
        for w in self.downstream_writers:
            try:
                w.write(data)
                await w.drain()
            except (ConnectionResetError, BrokenPipeError):
                dead.append(w)
        for w in dead:
            self.downstream_writers.remove(w)


class UpstreamPump:
    """One per (satellite, extra_output) - connects out to that specific
    satellite's flowgraph TCP_SERVER, retrying patiently whenever it's not
    running (i.e. between passes for THIS satellite), and forwards
    whatever arrives to whichever shared DownstreamListener it's attached
    to. Several pumps can share one listener."""

    def __init__(self, name, upstream_host, upstream_port, listener, verbose=False):
        self.name = name
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.listener = listener
        self.verbose = verbose

    def log(self, msg):
        print(f"[{self.name}] {msg}", flush=True)

    def vlog(self, msg):
        if self.verbose:
            self.log(msg)

    async def run(self):
        attempt = 0
        while True:
            attempt += 1
            try:
                if attempt == 1:
                    self.vlog(f"connecting upstream to {self.upstream_host}:{self.upstream_port} ...")
                reader, writer = await asyncio.open_connection(
                    self.upstream_host, self.upstream_port)
                self.log(f"connected upstream to {self.upstream_host}:{self.upstream_port}")
                attempt = 0
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await self.listener.broadcast(data)
                self.log("upstream connection closed (flowgraph likely exited at LOS) - "
                         "will keep retrying")
            except (ConnectionRefusedError, OSError) as e:
                # only the first failure and then an occasional heartbeat get
                # logged - between passes this retries every 2s for however
                # long the satellite is off the schedule, and printing every
                # single attempt would flood the terminal with nothing new to
                # say. HEARTBEAT_EVERY attempts (~1 minute at the default
                # retry delay) is a "still here, still waiting" confirmation
                # without drowning out anything actually worth seeing.
                if attempt == 1:
                    self.vlog(f"upstream not available yet ({e}) - retrying "
                              f"quietly every {RETRY_DELAY_S}s")
                elif attempt % HEARTBEAT_EVERY == 0:
                    self.vlog(f"still waiting for upstream ({attempt} attempts so far)")
            await asyncio.sleep(RETRY_DELAY_S)


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    listeners = {}   # bridge_port -> DownstreamListener
    pumps = []       # every UpstreamPump, regardless of which listener it feeds

    for sat in cfg.get("satellites", []):
        if not sat.get("enabled", True):
            continue
        for extra in sat.get("extra_outputs", []):
            if extra.get("protocol") != "tcp_bridge":
                continue
            bp = extra["bridge_port"]
            if bp not in listeners:
                listeners[bp] = DownstreamListener(bp, verbose=args.verbose)
            pump = UpstreamPump(f"{sat['name']}:{extra['name']}", "127.0.0.1",
                                 extra["port"], listeners[bp], verbose=args.verbose)
            pumps.append(pump)

    if not listeners:
        print("No enabled satellites configured with a tcp_bridge extra_output - "
              "nothing to do.", flush=True)
        return

    # a short, one-time warning - only for upstream ports genuinely shared by
    # more than one satellite, since that's the only case where TCP's lack of
    # any "who are you" concept can actually cause a mismatch: whichever
    # flowgraph happens to be running gets connected to and relayed under
    # every name sharing that port, with no way to detect if the wrong one
    # was left running
    by_upstream_port = {}
    for p in pumps:
        by_upstream_port.setdefault(p.upstream_port, []).append(p.name)
    for port, names in by_upstream_port.items():
        if len(names) > 1:
            print(f"NOTE: {', '.join(names)} all connect to upstream port {port} - "
                  f"make sure only the right flowgraph is running for whichever "
                  f"pass is active, since a bridge here can't tell them apart.",
                  flush=True)

    tasks = []
    for bp, listener in listeners.items():
        server = await asyncio.start_server(listener.handle_downstream, "127.0.0.1", bp)
        sharers = [p.name for p in pumps if p.listener is listener]
        print(f"[bridge:{bp}] listening on 127.0.0.1:{bp} for downstream consumers "
              f"- fed by: {', '.join(sharers)}", flush=True)
        tasks.append(asyncio.create_task(server.serve_forever()))

    for pump in pumps:
        tasks.append(asyncio.create_task(pump.run()))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    import station
    station.enter()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
