# groundtrack: Relay and Bridging

[← back to Architecture](architecture.md)

## The relay (for your downstream KISS decoder)

**Ownership: `relay.py` is a fully separate, standalone process. Nothing
else in this toolkit starts it for you, ever.** Not `run_passes.py`, not
`preflight.py`, not `test_downstream.py`. You start it once yourself at
the beginning of a session and leave it running - it's designed to
outlive every individual pass, which is the entire reason it exists (your
downstream decoder holds one stable connection across many AOS/LOS
cycles instead of reconnecting every time). If you already started it
earlier in your terminal session and never stopped it, it's still running
right now and every script below will use that same instance - you do not
need to restart it before every test.

| Process | Started by | Lifetime |
|---|---|---|
| `rigctld` | `run_passes.py`, automatically | same as `run_passes.py` |
| `rotctld` | you, manually | independent - you start/stop it |
| `relay.py` | you, manually | independent - persists across every pass |
| each `geoscanN.py` flowgraph | `run_passes.py`, one at a time | only alive during that satellite's AOS-to-LOS |

`preflight.py --live` and `test_downstream.py` both *use* `relay.py` if
it's already running (see [Troubleshooting](troubleshooting.md) for what happens if it
isn't) - neither one launches it.

Each `.grc` connects *out* to `relay.py` (`network_socket_pdu`, TCP_CLIENT)
instead of opening its own listening KISS port. `relay.py` opens two ports
per satellite (see `producer_port`/`consumer_port` in `satellites.yaml`):

- `producer_port` (9101-9106 for the shipped GEOSCAN flowgraphs: GEOSCAN-N uses 9100+N) - internal; only the currently-running
  flowgraph connects here. Nothing else should touch these.
- `consumer_port` (8101-8106, 8100+N) - point your decoder at these. This side
  never drops, even across LOS - only the producer side comes and goes as
  passes start and stop.

**Your downstream decoder needs one persistent connection PER SATELLITE
that uses the relay, all open at the same time - not one connection you
re-point between passes.** The relay serves every relay-using satellite's
ports simultaneously and continuously (it opens all of them - one
producer, one consumer, per satellite - in the same event loop at
startup, with no sequencing or switching between them at all). A single
decoder connection can only ever be on one port, so it will only ever
see one satellite's frames, no matter which satellite is actually
overhead at the time.

Concretely, in a tabbed decoder app like `SatsDecoder-linux`: open one
**tab per relay-using satellite**, each pointed at its own fixed
`consumer_port` (from `satellites.yaml`), and leave them all connected
indefinitely:

```
Tab "geoscan-1"  ->  127.0.0.1 : 8101   (connect once, leave open forever)
Tab "geoscan-2"  ->  127.0.0.1 : 8102   (connect once, leave open forever)
```

If your decoder app can't hold multiple independent connections in one
instance, run it as multiple separate OS processes instead, each with a
different Port set in its own window:
```
/path/to/decoder-binary &
/path/to/decoder-binary &
```

Once every tab is connected, nothing needs to be touched again between
passes - whichever satellite is actually overhead automatically lights
up its own tab/instance; the others just sit idle until it's their turn.
Verify they're actually connected by checking `relay.log` for one
`consumer connected` line per relay-using satellite that persists rather
than connect-then-disconnect.

Start `relay.py` and `run_passes.py` in either order - the flowgraph's
socket client retries until the relay is listening, and the relay's
consumer side accepts connections immediately even with no satellite up
(it'll just be silent until a pass starts).

To find which port a given satellite uses, either check `satellites.yaml`
directly, or just read `relay.py`'s own startup output:
```
[GEOSCAN-1] producer :9101  consumer :8101
[GEOSCAN-2] producer :9102  consumer :8102
```
(only satellites configured to use the relay show up here - recording-
only satellites are silently skipped, with a line saying so). Any port
number works as long as it's free and both ends (this config, and your
decoder's connection settings) agree on it - 8101/8102/... is just a
convenient, memorable scheme, not a requirement.

## The tcp_bridge (for a flowgraph running its own TCP_SERVER)

`relay.py` can only ever listen on both sides - every satellite that
uses it has a flowgraph connecting *out* to it as a client. Some
flowgraphs do the opposite: they run their own `TCP_SERVER` and wait for
a specific downstream app to connect *in* (an SSDV image viewer, say).
`tcp_bridge.py` exists for exactly that case - it's `relay.py`'s
mirror-image, connecting *out* to the flowgraph's `TCP_SERVER` as a
client (retrying patiently between passes, since the flowgraph only
exists while one's actually happening), while *listening* persistently
for the real downstream consumer, giving it one stable address for the
life of a session - the same thing a `SatsDecoder` tab gets from
`relay.py`.

**Ownership works exactly like `relay.py`**: a fully separate,
standalone process, never started by anything else in this toolkit.
Start it once at the beginning of a session and leave it running:
```
python3 tcp_bridge.py --verbose
```
Disabled satellites are skipped entirely, same as `relay.py`. In
`--verbose` mode, a retry that's failing repeatedly (normal between
passes, while the flowgraph isn't running) only logs its first attempt
and then an occasional "still waiting" heartbeat roughly once a minute -
not every single retry, which would otherwise flood the terminal for
however long the satellite is off the schedule.

**Multiple satellites can share one `bridge_port`** if they feed the
same downstream app - `ASRTU-1_SSDV` and `BY70-4` both do, since they
share the same SSDV viewer. `tcp_bridge.py` runs one real listening
socket per distinct `bridge_port`, with each satellite getting its own
independent, self-retrying connection to its own flowgraph's
`TCP_SERVER`. Since only one satellite's flowgraph is ever actually
running at a time (one shared SDR), only one of those upstream
connections is ever actually live in practice - the others just sit
retrying, harmlessly, until it's their turn. This is a genuine
improvement over separate ports, not just a shortcut: the downstream app
never needs its connection settings changed depending on which
satellite is about to pass.

**The real limitation this doesn't eliminate, confirmed the hard way**:
TCP has no concept of satellite identity, only ports. If two satellites
share an upstream `port` (not just `bridge_port`) - as `ASRTU-1_SSDV`
and `BY70-4` also do, both listening on `9985` - `tcp_bridge.py` cannot
verify that whatever accepted a connection on that port is actually the
satellite it thinks it's talking to. This isn't just a theoretical
both-somehow-live-at-once edge case; it showed up immediately in
ordinary manual testing, when only one flowgraph was intentionally
running and *both* satellites' upstream connections reported success -
because both really did connect to the one thing listening on `9985`,
regardless of which satellite's name was attached to that connection.
`tcp_bridge.py` prints a one-time warning at startup when it detects two
satellites sharing an upstream port, precisely because this can't be
caught any other way - verifying the right flowgraph is actually running
for whichever pass is active is entirely on you and `run_passes.py`, not
something a TCP-level bridge can ever check on its own.

Same as `relay.py`, `tcp_bridge.py` is one-directional and has zero
protocol awareness - it doesn't parse or care what's inside the bytes,
whether that's SSDV image data or anything else.

## Which one does a satellite actually use?

Not always obvious from just looking at `satellites.yaml`, so here's
the actual decision, with every real satellite in this fleet as an
example:

| Satellite has... | Uses `relay.py`? | Uses `tcp_bridge.py`? | Real example |
|---|---|---|---|
| `producer_port`/`consumer_port` set (feeds a live decoder like `SatsDecoder`) | Yes | No | `GEOSCAN-1..6` |
| No `producer_port`; `extra_outputs` with `protocol: tcp_bridge` | No | Yes | `ASRTU-1_SSDV`, `BY70-4`, `ASRTU-1_HYBRID` |
| No `producer_port`; `extra_outputs` are all `zeromq_pub` (or none at all) | No | No | `SCIONX` (recording-only) |
| `producer_port` set **and** a `tcp_bridge` extra_output | Yes | Yes | not in this fleet yet, but architecturally valid - e.g. a decode-and-relay satellite that *also* feeds a live SSDV viewer |

The two are completely independent - a satellite's `producer_port`
decides `relay.py`, and each individual `extra_outputs` entry decides
`tcp_bridge.py` for itself, so any combination is possible. `zeromq_pub`
never triggers either one, since nothing needs to bridge or relay it
(see [Adding a Satellite](adding-satellites.md) for why).

### Port numbering conventions

Not enforced by any tool - these are just the patterns this fleet has
settled on, worth following for anything new so ports stay predictable
at a glance:

| Piece | Pattern | Real examples |
|---|---|---|
| `producer_port` (`relay.py`, flowgraph's own `TCP_SERVER`) | `91xx`, one per satellite | `GEOSCAN-1..6`: `9101`-`9106` |
| `consumer_port` (`relay.py`, decoder connects here) | `81xx`, matching `producer_port`'s last two digits | `GEOSCAN-1..6`: `8101`-`8106` |
| `tcp_bridge`'s flowgraph-side port (`network_socket_pdu`, `TCP_SERVER`) | `99xx` | `ASRTU-1_SSDV`/`ASRTU-1_HYBRID`'s `ssdv_viewer`: `9986`; `ASRTU-1_HYBRID`'s `telemetry_decoder`: `9985` |
| `tcp_bridge`'s `bridge_port` (what the real downstream app connects to) | flowgraph-side port **+ 10000** | `9986` → `19986`; `9985` → `19985` |
| `zeromq_pub` address (untracked in `satellites.yaml` - see above) | `tcp://127.0.0.1:55xx` | `ASRTU-1_SSDV`: `5556`; `BY70-4`: `5555` |

The `+10000` pattern for `bridge_port` isn't a rule any tool checks -
it's just convenient, since the relationship between a flowgraph's own
port and what the downstream app actually connects to is visible at a
glance rather than needing to look it up.

### What each piece actually looks like in the `.grc`

A `relay.py`-fed `network_socket_pdu` and a `tcp_bridge`-fed one are
**visually identical blocks** - both `TCP_SERVER`, both connected to the
rest of the flowgraph the same way. The only thing that decides which
script actually uses a given block is which key in `satellites.yaml`
points at it - `producer_port` for `relay.py`, an `extra_outputs` entry
for `tcp_bridge.py`. There's nothing in the `.grc` itself that marks a
block as "the relay one" versus "the bridge one":

```yaml
# GEOSCAN-1's producer connection - relay.py reads producer_port: 9101
# to know this is the one it should connect to
- name: network_socket_pdu_0
  id: network_socket_pdu
  parameters:
    type: TCP_SERVER
    port: '9101'
    host: 127.0.0.1
```

```yaml
# ASRTU-1_HYBRID's ssdv_viewer connection - same block shape, but
# nothing here says "tcp_bridge" - that's purely a satellites.yaml
# extra_outputs entry pointing at this block by name
- name: network_socket_pdu_0
  id: network_socket_pdu
  parameters:
    type: TCP_SERVER
    port: '9986'
    host: 127.0.0.1
```

```yaml
# ASRTU-1_SSDV's telemetry output - a zeromq_pub_msg_sink instead,
# fire-and-forget, no satellites.yaml entry needed at all
- name: zeromq_pub_msg_sink_0
  id: zeromq_pub_msg_sink
  parameters:
    address: tcp://127.0.0.1:5556
    timeout: '1000'
```

And the two downstream pieces that actually read these values:

```yaml
# satellites.yaml - GEOSCAN-1, decode-and-relay
- name: GEOSCAN-1
  producer_port: 9101   # relay.py connects here as a client
  consumer_port: 8101   # SatsDecoder connects here
```

```yaml
# satellites.yaml - ASRTU-1_HYBRID, tcp_bridge
extra_outputs:
  - name: ssdv_viewer
    protocol: tcp_bridge
    block: network_socket_pdu_0   # matches the .grc block's name above
    port: 9986                    # matches the .grc block's own port
    bridge_port: 19985            # what the SSDV Viewer app connects to
```

