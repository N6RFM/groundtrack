# groundtrack: Adding a Satellite

[← back to README](../README.md)

## Adding a satellite

**Why nothing here touches `.grc` files.** Earlier versions of
`add_satellite.py` generated a new satellite's `.grc` automatically from
an existing one as a template. That repeatedly proved fragile in ways
GRC's own editor doesn't have - across three different satellites,
real bugs surfaced: a stray internal `id` field left mismatched with the
filename, `--record-only` leaving decoder/relay blocks present-but-
unconfigured instead of actually removing them, and an orphaned
`kiss_encode_pdu` block left disconnected on the canvas because it's an
embedded Python block sharing a generic type identifier with every other
embedded block. Each was fixable, but the pattern itself - a script
trying to safely manipulate GNU Radio's nested block-graph structure -
kept finding new ways to fail quietly. `satellites.yaml` is a simple flat
config format; a `.grc` is a complex graph GRC itself already knows how
to edit correctly. So the scope boundary is now firm: **every script
here only ever reads or writes `satellites.yaml`. Building or editing a
`.grc` - a brand new satellite, or a decoder/relay/`extra_outputs` block
inside one that already exists - is always a manual step in GRC.**
Copying an existing satellite's `.grc` as a starting point and adapting
it is a perfectly reasonable way to do that.

**`vet_grc.py`** is the one narrow, deliberate exception worth being
precise about, since it's easy to mistake for a reversal of the rule
above rather than a careful exception to it. Read-only inspection was
never in question - checking a `.grc`'s content is exactly what
`preflight.py` already does, and `vet_grc.py` extends that with checks
for the specific bugs this project has actually hit: GRC's own
copy/paste-collision renaming (a block ending up named
`network_socket_pdu_0_0` instead of `network_socket_pdu_0`), and the
`gpredict_doppler`-instead-of-`rig_freq_poller` mistake described below.
Its `--fix` flag *does* write to a `.grc`, but only for the two cases
that are a pure rename with no structural change at all - a block's own
name, or the flowgraph's `options.id` - never anything requiring a
decision about wiring. It refuses outright if a rename would collide
with a block that already exists, since that's a genuine duplicate
needing a human decision, not a misnamed single instance. Every fix
shows a diff before writing and keeps a `.bak` of the original. Missing
`rig_freq_poller`, a leftover waterfall, an incorrect `osmosdr_source`
tuning formula - none of that is something `--fix` will ever attempt;
those still mean opening GRC.

**Decode-and-relay** (has a `gr-satellites` decoder definition, feeds a
downstream decoder GUI over KISS):
```
python3 add_satellite.py --name GEOSCAN-3 --norad 64893 --freq 435742000
```

**Recording-only** (no decoder yet, or intentionally none - just raw IQ
to disk for later analysis):
```
python3 add_satellite.py --name SCIONX --norad 69880 --freq 437500000 --record-only
```
`--record-only` just means the config entry gets no
`producer_port`/`consumer_port` - that absence is what tells `relay.py`
and `preflight.py` this satellite has no relay involvement. The `.grc`
itself - no decoder, no KISS sink, no network block, `recordOnStart:
True`, no waterfall - is still yours to build in GRC; `scionx.grc` is a
working example to copy from.

**Direct-connection outputs** (`extra_outputs`) - for a satellite with a
second live output that a specific downstream app connects to, separate
from `relay.py`'s normal KISS path. There are three shapes this takes,
and which one applies depends entirely on which side is doing the
listening:

- **`zeromq_pub`** - the flowgraph publishes, the downstream app
  subscribes whenever it wants. Genuinely bypasses `relay.py` with
  nothing else needed: ZeroMQ PUB/SUB already solves the
  decouple-short-lived-producer-from-long-lived-consumer problem
  natively (a SUB socket connects, disconnects, and reconnects
  independently at any time), so routing it through `relay.py` - a plain
  TCP byte-forwarder with no protocol awareness of its own - would be
  solving a problem that protocol doesn't actually have.
- **`tcp_client`** - the flowgraph connects out, same direction
  `relay.py` itself expects, just to a different destination than
  `SatsDecoder`. Also a genuine direct bypass, no extra tooling needed.
- **`tcp_bridge`** - the flowgraph runs its own `TCP_SERVER` and waits
  for the downstream app to connect in. This is **not** a simple bypass
  - raw TCP has none of ZeroMQ's reconnection robustness, so without
  something in between, the downstream app has to detect the drop and
  reconnect at the start of every single pass. [`tcp_bridge.py`](#the-tcp_bridge)
  (below) solves this the same way `relay.py` solves the opposite
  direction: it connects out to the flowgraph's `TCP_SERVER` as a
  client, retrying patiently between passes, while listening
  persistently for the real downstream consumer - giving it one stable
  address for the life of a session.

`asrtussdv.grc` and `by704.grc` are the working examples - both flowgraphs
run a `network_socket_pdu` block as `TCP_SERVER` (an SSDV image viewer
needs to connect in) and a `zeromq_pub_msg_sink` block (a telemetry
upload agent subscribes independently):
```yaml
  - name: ASRTU-1_SSDV
    norad: 61781
    freq_hz: 436210000
    script: flowgraphs/asrtussdv.py
    min_elev_deg: 15
    extra_outputs:
      - name: ssdv_viewer
        protocol: tcp_bridge
        block: network_socket_pdu_0
        port: 9985           # the flowgraph's own TCP_SERVER
        bridge_port: 19985    # what the SSDV Viewer app actually connects to
      - name: telemetry_upload_agent
        protocol: zeromq_pub
        block: zeromq_pub_msg_sink_0
        address: "tcp://127.0.0.1:5556"
```
Multiple satellites feeding the *same* downstream app can share one
`bridge_port` - `tcp_bridge.py` runs one shared listener per distinct
port, with each satellite getting its own independent, self-retrying
upstream connection. Since only one satellite's flowgraph is ever
actually running at a time (one shared SDR), only one of those upstream
connections is ever actually live in practice, so this is both safe and
genuinely more convenient: the downstream app never needs its connection
settings changed depending on which satellite is about to pass.
`ASRTU-1_SSDV` and `BY70-4` both do exactly this, sharing `bridge_port:
19985` for the same SSDV viewer.

`preflight.py` validates each entry against the real `.grc` the same way
it validates `producer_port` - confirming the named block exists, that
its port (for `tcp_server`/`tcp_client`/`tcp_bridge`) or address (for
`zeromq_pub`) actually matches, and - for `tcp_bridge` specifically -
that `bridge_port` is set, plus fleet-wide uniqueness across every
`bridge_port` and `producer_port`/`consumer_port` (so two satellites
can *intentionally* share one, but never *accidentally* collide with
something else). `add_satellite.py` doesn't create these for you -
once the `.grc` is built, run
[`suggest_extra_outputs.py`](scripts-reference.md#suggest_extra_outputspy)
NAME: it scans the real `.grc` for network-facing blocks not yet
claimed by an extra_output, and drives `edit_satellite.py` with the
port/address/block-name read directly from the file - the only things
it asks for are a name and, for `tcp_bridge`, a `bridge_port`, since
those are genuine decisions rather than facts the `.grc` already
contains. `edit_satellite.py` (below) or the GUI's Edit dialog remain
the way to adjust or remove an entry afterward, or to copy an existing
satellite's outputs as a starting point when a new satellite shares the
same downstream app (as ASRTU-1_SSDV and BY70-4 both do).
Either way, the `.grc`'s actual block - its name, port, or address -
still has to be built or edited separately in GRC to match; nothing here
touches `.grc` content.

Either way, finish with:
```
grcc flowgraphs/<name>.grc      # or ./regen_all.sh for everything at once
python3 update_tle.py           # auto-covers every configured satellite, no flags needed
python3 preflight.py
```

**Editing a satellite** already in `satellites.yaml` - NORAD, frequency,
min elevation, relay ports, enabled state, or its `extra_outputs` list:
```
python3 edit_satellite.py GEOSCAN-1 --freq 435970000
python3 edit_satellite.py GEOSCAN-1 --producer-port 9110 --consumer-port 8110

python3 edit_satellite.py ASRTU-1_SSDV --extra-output-name ssdv_viewer \
    --extra-output-protocol tcp_bridge --extra-output-block network_socket_pdu_0 \
    --extra-output-port 9985 --extra-output-bridge-port 19985
python3 edit_satellite.py ASRTU-1_SSDV --remove-extra-output ssdv_viewer
```
Only touches the fields you actually pass, and only ever `satellites.yaml`
- never the `.grc`. If a change here (a new frequency, a corrected
NORAD) needs the `.grc` to match, that's still your own separate edit in
GRC; the script prints a note when that applies. Refuses a NORAD or port
collision with another configured satellite rather than silently
creating one. Adding an `extra_outputs` entry with a name that already
exists on that satellite replaces it rather than duplicating it, so
re-running the same command with a corrected value is safe.

**Toggling IQ recording on or off per run** (`record_iq_toggle`) - for a
satellite whose `.grc` uses
[gr-filerepeater_n6rfm](https://github.com/N6RFM/gr-filerepeater_n6rfm)'s
`Advanced File Sink` block with `Record On Start` set to the expression
`bool(record_iq)` (a Parameter block, not a fixed literal). This is a
fork of upstream [gr-filerepeater](https://github.com/ghostop14/gr-filerepeater)
specifically because upstream's `Record On Start` is a locked Yes/No
dropdown with no way to reference a variable - the fork changes that one
field's type so it can hold an expression instead. `record_iq_toggle` in
`satellites.yaml` is a persistent capability flag confirming a
satellite's `.grc` is wired this way; the actual record-or-not decision
is a session-wide choice made when `run_passes.py` starts, not stored
anywhere:
```
python3 edit_satellite.py ASRTU-1_SSDV --record-iq-toggle
python3 edit_satellite.py ASRTU-1_SSDV --no-record-iq-toggle
```
See [run_passes.py](scripts-reference.md#run_passespy) for the
`--record-iq` flag this actually enables.

**Pausing a satellite** without deleting its hard-won config - sharing
one SDR across satellites you don't all want active at once is the
normal case, not an edge case:
```
python3 toggle_satellite.py --list
python3 toggle_satellite.py --disable GEOSCAN-1
python3 toggle_satellite.py --enable GEOSCAN-1
```
A disabled satellite is invisible to `relay.py`, `run_passes.py`,
`plan_passes.py`, and `preflight.py`'s live checks - as if it weren't in
`satellites.yaml` at all - while its full config sits untouched, ready
to re-enable with no reconfiguration. Re-run `plan_passes.py` after
toggling anything, since it fully regenerates `schedule.yaml` from
whatever's currently enabled - a disabled satellite's old approved
passes simply won't be in the new file, nothing to prune by hand.

## Satellite data

Frequencies, ports, and every other per-satellite setting live in
`satellites.yaml` - that file is the single source of truth, not this
README. Pull current downlink frequencies from db.satnogs.org before a
real pass regardless of what's already configured; drift of several kHz
between checks isn't unusual.

For a satellite with no decoder yet, no `producer_port`/`consumer_port`
is needed at all - see "Adding a satellite" below.

