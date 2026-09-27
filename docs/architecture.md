# groundtrack: Architecture

[← back to README](../README.md)

## How the pieces fit together

Every satellite runs a GNU Radio flowgraph - that part's universal.
Everything after it is optional and varies per satellite; no single
downstream tool is required for all of them:

```
GNU Radio flowgraph
(one process per pass, launched fresh
 at AOS by run_passes.py, exits at LOS)
        |
        |-- decode-and-relay satellites (GEOSCAN-1..6) --------------------
        |     |
        |     v
        |   relay.py                        SatsDecoder
        |   (one long-running process   --> (stays open, one tab per
        |    per satellite, started         satellite, connected once -
        |    once per session - only        this is what GEOSCAN happens
        |    for satellites configured       to feed; a different relay-
        |    to use it)                       using satellite could feed
        |                                     a different decoder entirely,
        |                                     since relay.py is protocol-
        |                                     agnostic - see below)
        |
        |-- extra_outputs satellites (ASRTU-1_SSDV, BY70-4) -----------
        |     |
        |     v
        |   extra_outputs, two genuinely different shapes:
        |     - zeromq_pub: flowgraph publishes, app subscribes whenever
        |       it wants - fire-and-forget, nothing in between, relay.py
        |       never involved (untracked in satellites.yaml entirely -
        |       see Adding a Satellite)
        |     - tcp_bridge: flowgraph's own TCP_SERVER, but the real app
        |       (an SSDV image viewer) needs to stay "connected" across
        |       every pass, not reconnect at each AOS - tcp_bridge.py
        |       sits in between for exactly this, connecting out to the
        |       flowgraph as a client while listening persistently for
        |       the real app (see below)
        |
        |-- recording-only satellites (SCIONX) -----------------------------
              |
              v
            raw IQ to disk only - no relay, no live decoder, no
            extra_outputs, nothing downstream but a file
```

Both `relay.py` and any live downstream decoder are per-satellite
choices, not fixed parts of the architecture - and even among satellites
that *do* use `relay.py`, `SatsDecoder` specifically is just what
GEOSCAN's flowgraphs happen to feed today, not something the toolkit
requires. A different relay-using satellite could just as easily feed an
entirely different decoder application.

**The flowgraph** is generated per satellite from that satellite's
`.grc` file. For satellites with a decoder, it demodulates the live
signal and hands decoded frames to a few destinations at once - a
`.kss` file on disk, a console printout, and a live network connection.
For recording-only satellites, it just tunes, corrects for Doppler, and
writes raw IQ to disk - no decoder, no KISS output, no network
connection at all.

**A downstream decoder** sits at the end of that network connection, for
satellites configured to use one. For GEOSCAN, that's
[SatsDecoder](https://github.com/baskiton/SatsDecoder) - an existing,
general-purpose open-source project, not something written for this
project, and not a fixed part of this toolkit's architecture. It decodes
frames for a range of amateur/university cubesat protocols - GEOSCAN,
USP (Unified SPUTNIX), AX.25, and CSP (Cubesat Space Protocol) among
them - via YAML satellite definitions, and gives a persistent
per-satellite tab with a live history of decoded frames over a KISS TCP
link. See [its own supported-protocols list](https://github.com/baskiton/SatsDecoder)
for the full and current set, since it grows independently of this
project. It's what GEOSCAN's flowgraphs happen to feed today - not a
requirement `relay.py` or anything else in this toolkit imposes. Earlier
issues found in it during this project's development (a false-disconnect
bug, and a KISS-timestamp
`OverflowError`) have both since been fixed in upstream's `nightly`
branch - confirmed directly against commit `2112e3f` ("#7 catch
overflow error when parsing KISS-timestamp"), sitting on top of the
`d94ff8e` refactor that introduced it. Run from `nightly`, not `main`;
it's unclear as of this writing when or whether these land in `main`.

**A different satellite can feed a completely different downstream
consumer, on either side of `relay.py`, using an entirely different
protocol - `relay.py` doesn't know or care.** It has no KISS awareness
at all: it's a pure byte-forwarding TCP proxy, nothing more. Whatever
arrives on a satellite's producer port gets forwarded verbatim to its
consumer port, with no parsing, no framing logic, and no protocol
knowledge of any kind - proven directly by the byte-for-byte fidelity
test earlier in this project's development, which showed `relay.py`
forwards exactly what it receives regardless of whether those bytes are
correctly KISS-framed, unframed, or something else entirely. That's
precisely why the `kiss_encode_pdu` bug (below) was possible in the
first place: `relay.py` was never responsible for that framing, and
nothing about it would have complained either way. A satellite's `.grc`
could just as easily feed `relay.py` a custom telemetry protocol, raw
samples, or anything else TCP can carry, to a downstream consumer that
has nothing to do with KISS or SatsDecoder at all - the only real
requirement is that whatever sits on both ends of a given port pair
agrees with each other, the same way a network cable doesn't need to
understand the packets running through it.

**`relay.py`** sits between the flowgraph and its downstream consumer,
for satellites that use both, because they have very different
lifecycles. The flowgraph is short-lived - `run_passes.py` launches it
fresh at every AOS and it exits at LOS, every pass, every satellite,
independently. A decoder GUI like SatsDecoder, by contrast, is meant to
be left open with each satellite's tab connected once and left alone; it
doesn't expect the far end of that connection to disappear and reappear
every few minutes. Without something in between, either the downstream
side would need to detect and reconnect around every single pass
boundary itself, or the flowgraph would need to somehow wait for it to
be listening before it could start. `relay.py` decouples the two
entirely: it's a single long-running process per satellite, started once
at the beginning of a session, that the short-lived flowgraph connects
*out* to as a client at every AOS, and that the downstream side connects
*in* to once and leaves alone. Either side can restart independently - a
flowgraph crashing mid-pass, or a decoder tab getting disconnected -
without the other one needing to know or care. Its TCP keepalive and
per-consumer diagnostic logging (see "The relay" section below) exist
specifically to make that long-lived middle process itself trustworthy,
since if it ever silently died or lost track of a connection, both
sides would be depending on a link that no longer worked without either
one finding out.

**Recording-only satellites skip the last two pieces entirely.** A
satellite with no `producer_port`/`consumer_port` in `satellites.yaml`
has no relay involvement and no decoder requirement - `relay.py` skips
it, `preflight.py`'s relay-specific checks skip it, and its flowgraph
just records raw IQ for you to analyze or decode later, whenever a
decoder for it exists. This is a first-class, fully supported mode, not
a workaround - see [Adding a satellite](adding-satellites.md).

**Using the relay is a real choice with a real tradeoff, not just "on
for satellites with a decoder, off for satellites without one."**
Skipping it entirely is simplest when there's genuinely nothing waiting
on the other end yet - one less process to run, one less thing that can
go wrong. But the moment something *is* meant to consume frames live,
during the pass rather than after it, going through `relay.py` buys you
real robustness that a flowgraph connecting straight to a decoder
doesn't have: the decoder can hold one stable connection across many
AOS/LOS cycles instead of reconnecting every pass, either side can
restart independently without the other needing to notice, and TCP
keepalive plus diagnostic logging catch a dead link rather than leaving
a silent zombie connection. Bypass the relay for a satellite that does
have a live downstream consumer, and you take on all of that yourself -
the decoder now needs to detect and reconnect around every pass boundary
on its own, and a flowgraph that dies mid-pass just leaves the decoder
hanging with no signal anything went wrong.

## Folder layout

```
groundtrack/
├── satellites.yaml       # ground station + per-satellite settings (edit this)
├── schedule.yaml          # generated by plan_passes.py - approved pass list
├── setup_station.py       # wizard: sets lat/lon/alt, TLE source, rig port
├── plan_passes.py         # predicts passes, lets you approve/reject them
├── run_passes.py          # executor: launches flowgraphs at AOS, feeds Doppler+rotor
├── relay.py                # persistent TCP relay, for satellites configured to use one
├── show_queue.py            # prints the approved pass queue from schedule.yaml
├── doctor.py                # one command: environment + config sanity check
├── preflight.py             # config checks + optional --live flowgraph launch
├── test_downstream.py       # pushes test frames through the relay to your decoder(s)
├── send_test_frames.py      # single-satellite version test_downstream.py builds on
├── locate_decoders.py       # finds & patches decoder .yml paths in your .grc files
├── add_satellite.py         # adds a new satellite's config entry (you build the .grc)
├── edit_satellite.py        # edits an existing satellite's fields (and extra_outputs)
├── toggle_satellite.py      # enable/disable a satellite without deleting its config
├── update_tle.py            # refreshes tle_file - SatNOGS primary, Celestrak fallback
├── ci_check.py              # portable checks - what CI runs on every push
├── regen_all.sh             # grcc every .grc, then run preflight.py
├── Makefile                 # make build / check / status / doctor
├── requirements.txt
├── .github/workflows/ci.yml # runs ci_check.py on every push/PR
├── flowgraphs/
│   ├── kiss_encode_pdu.py  (reference copy - GRC embeds this as text in each .grc)
│   ├── geoscan1.grc / .py  (decode-and-relay satellite; .py generated by you, see Setup)
│   ├── geoscan2.grc / .py
│   ├── geoscan3.grc / .py
│   ├── geoscan4.grc / .py
│   ├── geoscan5.grc / .py
│   ├── geoscan6.grc / .py
│   ├── scionx.grc / .py    (recording-only satellite - no decoder, no relay, see below)
│   └── asrtussdv.grc / .py (recording-only, with extra_outputs - two
│                             consumers bypassing relay.py entirely, see below)
├── groundtrack_gui.py     # optional GUI over the CLI tools - see GUI.md
├── delete_satellite.py     # removes a satellite's config entry (files untouched)
├── vet_grc.py              # vets a .grc for known copy/paste and Doppler bugs
├── tcp_bridge.py           # relay.py's mirror-image, for flowgraphs running a TCP_SERVER
└── tle/
    └── amateur.txt         # generated by update_tle.py - gitignored, not committed
```

## Why every `.grc` needs a `kiss_encode_pdu` block

**Every flowgraph's `network_socket_pdu` block must go through the
`kiss_encode_pdu` embedded Python block before reaching the socket, not
be wired directly to the satellite decoder's `out` port.** Without it,
frames arrive at SatsDecoder corrupted or missing entirely, even though
the same pass looks completely normal in the console and in the `.kss`
file on disk.

**Root cause:** `satellites_satellite_decoder`'s PDU output is raw,
unframed payload bytes - nothing more. `satellites_kiss_file_sink`
applies real KISS framing (`0xC0` FEND delimiters, with `0xDB`/`0xDC`/
`0xDD` escaping) internally when it writes those bytes to a `.kss` file,
which is why every `.kss` file this station has ever produced parses
correctly. `network_socket_pdu`, however, is a generic core GNU Radio
block with no knowledge of KISS at all - it just writes whatever PDU
bytes it's handed straight to the TCP socket, unframed. So the exact
same PDU stream gets framed on the file path and left completely
unframed on the live network path, from the same decoder, at the same
instant.

**Why this was so hard to catch:** SatsDecoder's `kiss_read_stream()`
looks for a `0xC0` byte to find a frame boundary. If a byte chunk has no
`0xC0` in it at all, that function's own logic silently discards it as
"garbage" - no error, no warning, frame count just doesn't move. When
frames arrive spaced far enough apart in time, each one *sometimes*
happens to land in its own TCP read anyway and gets discarded quietly,
which is easy to miss. But under a fast burst (e.g. mid-image
transmission, dozens of frames in a couple of seconds), the OS coalesces
several unframed PDUs into one `recv()`, and SatsDecoder's parser can
mis-lock onto some byte pattern inside that blob and misreport it as one
giant, garbage frame - which is what actually happened during the
2026-09-20 19:27 GEOSCAN-2 pass: a single "frame" reported as
`INNOSAT16`, `1144` bytes long, when it should have been dozens of
separate 72-byte GEOSCAN frames. Confirmed directly: repeating the exact
same session prefix appeared every **72 bytes**, back to back, with zero
`0xC0` delimiters anywhere between them, for as long as the burst
lasted - proof the frames were real and correctly decoded, just never
framed for the network.

**The fix:** `kiss_encode_pdu` (kept in this repo at
`flowgraphs/kiss_encode_pdu.py` for reference and diffing, since GRC
embeds its actual code as text inside the `.grc` file itself) wraps each
PDU exactly the way `kiss_file_sink` already does - FEND byte, KISS
command byte, escaped payload, FEND byte - before it reaches
`network_socket_pdu`. It changes nothing else: the decoder's connections
to Print Timestamp, KISS File Sink, and Telemetry Submit are untouched;
only the network path gets the extra hop.

```
satellite_decoder (out)  --------------------------->  kiss_file_sink       (already framed internally)
                          --------------------------->  print_timestamp / telemetry_submit
                          ---> kiss_encode_pdu (in/out) ---> network_socket_pdu (pdus)   <- the fix
```

**Verifying it's actually working**, without needing a live pass or any
hardware: wire `KISS File Source` (reading any real `.kss` file this
station has already captured) through `kiss_encode_pdu` into
`network_socket_pdu`, run it, and connected SatsDecoder tabs should show
individual, correctly-sized frames appearing one at a time. Removing
`kiss_encode_pdu` from that same test rig reliably reproduces the exact
original failure - one giant misparsed frame, then nothing further -
confirming the block is what fixes it rather than something else
changing at the same time.

**Every decode-and-relay satellite's `.grc` needs this** - all of them
share the same `satellite_decoder` -> `network_socket_pdu` pattern, so
any of them can have this gap. Add `kiss_encode_pdu` to each, then
`./regen_all.sh` to recompile all of them at once. Recording-only
satellites don't have `network_socket_pdu` at all, so this doesn't apply
to them.

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

- `producer_port` (9101-9104) - internal; only the currently-running
  flowgraph connects here. Nothing else should touch these.
- `consumer_port` (8101-8104) - point your decoder at these. This side
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

## How Doppler control works

- `run_passes.py` starts **one** `rigctld -m 1 -t <rig_port>` (Hamlib's
  Dummy rig backend - just a frequency register, no real hardware behind
  it) when it launches, and keeps it running for the whole session,
  independent of which satellite is active.
- `run_passes.py` is the only thing that ever *sets* frequency on it
  (`F <hz>`, computed live from the TLE via Skyfield).
- Whichever satellite's flowgraph is currently running *polls* that same
  rigctld for the current frequency (`f`) via an embedded Python block
  (`rig_freq_poller_0`) and feeds it into a GRC variable (`freq`) via a
  `Message Pair to Variable` block.
- Because there's exactly one rigctld for the whole fleet and only one
  flowgraph is ever alive at a time (single SDR), there's no per-satellite
  port to manage - every `.grc` points at the same `127.0.0.1:<rig_port>`.
- This is genuine Hamlib, not a custom protocol, so a real copy of
  Gpredict's radio control window can point at the same address (as a NET
  rigctl rig) purely for a visual read-out if you want one.

**What actually happens to that `freq` variable once it updates** - this
is the part worth understanding fully, since it's easy to assume Doppler
correction means re-tuning the SDR hardware itself, and that's not what
happens here. Look at `osmosdr_source_0`'s own tuned frequency in any
working `.grc` (`geoscan1.grc` is the reference): its value is a fixed
formula, `nfreq - offset` - built only from `nfreq` (the satellite's
*nominal*, unchanging frequency) and `offset` (a fixed DC-avoidance
offset, `50e3` Hz by convention). **The SDR hardware is tuned exactly
once, at flowgraph startup, and never retuned for the rest of the
pass** - deliberately, since continuously re-tuning real hardware
mid-pass risks PLL relock glitches and settling delays at exactly the
moment you can least afford to lose samples. All the actual Doppler
tracking happens one stage later, in the Signal Source block
(`analog_sig_source_x_0_0`) feeding a `Multiply` block: its frequency is
`-(freq-nfreq+offset)+BFO` - it *does* reference the live `freq`
variable, and continuously recalculates the mixing frequency to shift
the signal by exactly the live Doppler offset, entirely in software,
after the fixed-frequency hardware has already captured it. As long as
the shifted signal stays inside the low-pass filter's passband (25 kHz
by convention here, comfortably wider than the few-kHz shift a typical
LEO pass produces at these frequencies), this works correctly and avoids
ever touching the hardware mid-pass at all.

**The one real pitfall, confirmed the hard way on a real satellite:**
`rig_freq_poller` is not the only block that can feed a `Message Pair to
Variable` block, and the other common one looks superficially similar
but does something completely different. `gpredict_doppler` is part of
the same OOT module and produces the same kind of message output - but
it's a *passive listener*, waiting for an actual instance of the
Gpredict application to connect to it and push frequency updates over
Gpredict's own native rig-control protocol. This toolkit never plays
that role - `run_passes.py` only ever pushes frequency *into* `rigctld`
as a Hamlib client; it never connects *out* as a Gpredict client to
anything. A flowgraph wired with `gpredict_doppler` instead of
`rig_freq_poller` will run with no errors, tune correctly at pass start,
and then silently receive zero Doppler correction for the entire pass -
the console just shows `[doppler] Waiting for connection on:
127.0.0.1:<port>` for the whole thing, since nothing ever will connect.
**Always use `rig_freq_poller`, matching `geoscan1.grc`'s wiring exactly**
- if a satellite's `.grc` was built by copying an unrelated gr-gpredict
example rather than an existing satellite in this fleet, check this
specifically before trusting a pass's Doppler correction.

## Antenna control

`run_passes.py` optionally drives a rotor through `rotctld`, the same
pattern as Doppler - one persistent client connection fed by whichever
satellite is active. Unlike `rigctld`, you start `rotctld` yourself, since
it needs your actual serial port and rotor model:
```
rotctld -m 607 -r /dev/ttyUSB2
```
(no `-t` needed - `rotctld` defaults to port 4533, matching `rot_port` in
`satellites.yaml`.) Start it before or any time up to the first pass - the
client reconnects automatically once it's up.

To run with no rotor at all, remove `rot_host`/`rot_port` from
`satellites.yaml`; `run_passes.py` prints a note and skips antenna control.

Az/el updates use lead-ahead targeting, ported from a proven, working
implementation ([N6RFM/Gpredict_K4KDR_N6RFM](https://github.com/N6RFM/Gpredict_K4KDR_N6RFM)'s
own rotor controller): a new position is only sent once the satellite has
drifted `threshold_deg` (default `5.0`) from wherever the rotor was last
commanded, and when it is sent, it's not the satellite's instantaneous
position but a predicted future point - found by binary-searching for the
furthest time (up to the pass's own LOS) where the satellite still sits
within `threshold_deg` of its current position. This gives the rotor a
real destination to travel toward continuously, rather than repeatedly
redirecting it toward a target that's already stale by the time each
command lands. There is no time-based forced send at all - a slow-moving
stretch of a pass can go a long time between commands, which is correct,
not a gap. See `find_lead_ahead_target()` and `maybe_update_rotor()` in
`run_passes.py`; adjust `threshold_deg` there if your rotor wants finer
or coarser steps.

