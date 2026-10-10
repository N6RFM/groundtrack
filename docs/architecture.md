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
├── pass_log.jsonl         # generated by run_passes.py - real outcome of every pass run (local, gitignored)
├── setup_station.py       # wizard: sets lat/lon/alt, TLE source, rig port
├── plan_passes.py         # predicts passes, lets you approve/reject them
├── run_passes.py          # executor: launches flowgraphs at AOS, feeds Doppler+rotor
├── relay.py                # persistent TCP relay, for satellites configured to use one
├── show_queue.py            # prints the approved pass queue from schedule.yaml
├── station.py               # multi-radio setups: picks which station's folder a script works in
├── tle_util.py              # which TLE a satellite uses: custom_tle_file's entry always beats tle_file's
├── tle_number.py            # catalog number from a TLE line, incl. Alpha-5 (A0470 = 100470) for numbers >= 100000; OMM->TLE conversion; relabel_tle for SatNOGS temporary IDs
├── lanes.py                 # receivers (Doppler channels) and paired passes - shared rules
├── pair_passes.py           # pair overlapping passes on two receivers; choose whose TLE steers the beam
├── set_doppler_ports.py     # point flowgraphs' Doppler pollers at the right rigctld port, by the SDR each opens
├── radios.example.yaml      # copy to radios.yaml to switch multi-station mode on
├── migrate_to_stations.py   # one-time: classic layout -> one folder per station (see stations.md)
├── wire_record_iq.py        # wires a .grc for the per-run IQ toggle (diff + backup)
├── toggle_pass_record_iq.py # per-pass IQ recording override in the queue
├── show_pass_log.py         # summarizes pass_log.jsonl - what actually happened per pass
├── doctor.py                # one command: environment + config sanity check
├── preflight.py             # config checks + optional --live flowgraph launch
├── test_downstream.py       # pushes test frames through the relay to your decoder(s)
├── send_test_frames.py      # single-satellite version test_downstream.py builds on
├── locate_decoders.py       # finds & patches decoder .yml paths in your .grc files
├── add_satellite.py         # adds a new satellite's config entry (you build the .grc)
├── new_record_only_satellite.py  # .grc-from-template + add_satellite.py + grcc, one step
├── edit_satellite.py        # edits an existing satellite's fields (and extra_outputs)
├── toggle_satellite.py      # enable/disable a satellite without deleting its config
├── update_tle.py            # refreshes tle_file - SatNOGS primary, Celestrak fallback
│                             #   (custom_tle_file, if set, is read but never written to)
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
    ├── amateur.txt         # generated by update_tle.py - gitignored, not committed
    └── custom.txt          # optional, hand-maintained - for a satellite too new
                             #   for SatNOGS/Celestrak; name is up to you, set as
                             #   custom_tle_file in satellites.yaml
```

With `radios.yaml` (see [Stations](stations.md)), the project root keeps only
the scripts, `tle/` and `radios.yaml`; `satellites.yaml`, `schedule.yaml`,
`pass_log.jsonl` and `flowgraphs/` above live inside a station folder instead
(`beam/`, `helix/`), laid out exactly like the old root.

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


## See also

- [Relay and Bridging](relay-and-bridging.md) - `relay.py`, `tcp_bridge.py`, which one a given satellite actually uses, port conventions, and real `.grc` block examples
- [Tracking Control](tracking-control.md) - how Doppler and antenna tracking actually work, the lead-ahead rotor design, and how to measure and configure your own rotor's real limits
