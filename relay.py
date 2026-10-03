#!/usr/bin/env python3
"""
Persistent KISS relay.

Runs forever (independent of any satellite pass). For each satellite it
opens two listening TCP ports:

  - producer_port: the currently-running per-satellite .grc connects INTO
    this as a TCP client (blocks_socket_pdu, TCP_CLIENT) and pushes KISS
    frame bytes.
  - consumer_port: your downstream decoder/dashboard connects INTO this
    (same port it already expects) and receives whatever the relay
    forwards. This socket never closes just because a pass ends - only
    the producer side comes and goes.

Run this once, alongside run_passes.py, before any pass starts.

Keep-alive strategy (added after one of four parallel consumer
connections kept silently dropping, requiring manual reconnection):

  1. TCP-level keepalive (SO_KEEPALIVE + tuned intervals on Linux) on
     every accepted socket - catches a genuinely dead connection (or a
     NAT/firewall silently killing an idle one) much faster than the
     OS default, which can take hours. This sends no application-level
     bytes at all, so it cannot confuse any KISS parser.

  NOTE: an earlier version of this file also sent a periodic empty KISS
  frame (FEND FEND) as an application-level heartbeat. That was removed
  after tracing SatsDecoder's own kiss_read_stream()/​_receive() code:
  an empty/malformed KISS frame is indistinguishable from a genuine
  disconnect in that client (both produce a falsy `frame`, triggering
  its "Connection lost" path) - so the heartbeat was actively causing
  the disconnects it was meant to prevent. TCP keepalive alone is safe
  because it never touches the byte stream the KISS parser sees.
"""

import asyncio
import socket
import sys
import time
import yaml

CONFIG_PATH = "satellites.yaml"


def tune_keepalive(writer):
    """Enable TCP keepalive with aggressive intervals so a dead/idle
    connection is detected in ~35s instead of the OS default (often
    hours) - the leading suspect for a connection silently going stale
    with no error on either end until the next real write."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform.startswith("linux"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)   # start probing after 10s idle
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)  # probe every 5s
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)    # 5 failed probes = dead
    except OSError:
        pass  # best-effort; some platforms/socket types don't support all options


class SatRelay:
    def __init__(self, name):
        self.name = name
        self.consumers = set()  # set of writers
        self.last_data_sent = {}  # writer -> monotonic time of last forwarded byte

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{self.name}] {msg}", flush=True)

    async def handle_producer(self, reader, writer):
        tune_keepalive(writer)
        peer = writer.get_extra_info("peername")
        self.log(f"producer connected: {peer}")
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                now = time.monotonic()
                dead = set()
                for w in self.consumers:
                    try:
                        w.write(data)
                        await w.drain()
                        self.last_data_sent[w] = now
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        dead.add(w)
                self.consumers -= dead
        finally:
            self.log(f"producer disconnected: {peer}")
            writer.close()

    async def handle_consumer(self, reader, writer):
        tune_keepalive(writer)
        peer = writer.get_extra_info("peername")
        connected_at = time.monotonic()
        self.log(f"consumer connected: {peer}")
        self.consumers.add(writer)
        self.last_data_sent[writer] = connected_at
        try:
            while not reader.at_eof():
                await reader.read(4096)  # discard anything the consumer sends
        except (ConnectionResetError, OSError) as e:
            self.log(f"consumer {peer} error: {e!r}")
        finally:
            uptime = time.monotonic() - connected_at
            idle = time.monotonic() - self.last_data_sent.get(writer, connected_at)
            self.log(f"consumer disconnected: {peer} "
                     f"(connected {uptime:.1f}s, idle {idle:.1f}s before drop)")
            self.consumers.discard(writer)
            self.last_data_sent.pop(writer, None)
            writer.close()


async def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    servers = []
    for sat_cfg in cfg["satellites"]:
        if not sat_cfg.get("enabled", True):
            print(f"[{sat_cfg['name']}] disabled - skipping", flush=True)
            continue
        if "producer_port" not in sat_cfg and "consumer_port" not in sat_cfg:
            print(f"[{sat_cfg['name']}] no producer_port/consumer_port configured - "
                  f"skipping (recording-only satellite, no relay involvement)", flush=True)
            continue
        relay = SatRelay(sat_cfg["name"])
        prod_srv = await asyncio.start_server(
            relay.handle_producer, "127.0.0.1", sat_cfg["producer_port"])
        cons_srv = await asyncio.start_server(
            relay.handle_consumer, "127.0.0.1", sat_cfg["consumer_port"])
        servers += [prod_srv, cons_srv]
        print(f"[{sat_cfg['name']}] producer :{sat_cfg['producer_port']}  "
              f"consumer :{sat_cfg['consumer_port']}  (TCP keepalive on)", flush=True)

    if not servers:
        print("No satellites configured for relay - nothing to do. "
              "(All satellites are recording-only, or satellites.yaml is empty.)",
              flush=True)
        return

    async with servers[0]:
        await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    import station
    station.enter()
    asyncio.run(main())
