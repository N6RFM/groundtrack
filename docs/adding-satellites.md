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
to edit correctly. So the scope boundary is firm: **no script here
builds or designs a `.grc` - a brand new satellite, or a decoder/relay/
`extra_outputs` block inside one that already exists, is always a manual
step in GRC.** Three narrow, single-purpose tools do make small mechanical
edits to an existing `.grc` (a rename, a decoder path, one wired
parameter) - described below - and none of them creates or changes a
connection between blocks. Everything else here only ever reads or writes
`satellites.yaml`.
Copying an existing satellite's `.grc` as a starting point and adapting
it is a perfectly reasonable way to do that.

**`vet_grc.py`**, **`locate_decoders.py`**, and **`wire_record_iq.py`** are
the narrow, deliberate exceptions worth being precise about, since they're easy to
mistake for a reversal of the rule above rather than careful exceptions
to it. Read-only inspection was never in question - checking a `.grc`'s
content is exactly what `preflight.py` already does, and `vet_grc.py`
extends that with checks for the specific bugs this project has actually
hit: GRC's own copy/paste-collision renaming (a block ending up named
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

`locate_decoders.py` is the other exception, and a narrower one: it only
ever sets the `file` parameter on an existing `satellites_satellite_decoder`
block, given a decoder `.yml` it found by searching a directory tree for
a name or NORAD match - see [scripts-reference.md](scripts-reference.md)
for the full usage. Worth knowing before running it: unlike `vet_grc.py
--fix`'s careful raw-text editing, this one round-trips the whole file
through PyYAML (load, modify, dump), which will reformat the entire
`.grc`'s structure as a side effect - coordinates and formatting may
shift even though nothing about the flowgraph's actual wiring changes.
Review the diff before committing, same as any other `.grc` change.

`wire_record_iq.py` is the third, and the most careful about how it edits:
it changes exactly one line (the Advanced File Sink's Record On Start) and
inserts one block (the `record_iq` Parameter block) as *raw text*, never a
load/dump round-trip, so nothing else in the file is reformatted. It shows a
diff and asks first, keeps a `.bak`, re-checks its own result (restoring the
original if the check fails), refuses anything that needs a human decision,
and does nothing to a flowgraph that's already wired. See "Toggling IQ
recording" below and [scripts-reference.md](scripts-reference.md).

**If the flowgraph is open in GRC when any of these three runs, close it
first - discarding, not saving - and reopen it afterwards.** GRC doesn't
notice a file changing underneath it: it keeps showing the copy it
loaded, so the edit looks like it never happened, and saving (or
Generate/Run, which rebuilds the `.py` from what's on screen) silently
writes that older copy back over the tool's change. The edit is on disk
either way; a stale window is just a stale view - but a dangerous one to
act on.

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

Either way, the new entry is always written as `enabled: false` - nothing
is built yet, and a *missing* `enabled` key is treated as `true`
everywhere else in this toolkit, which would make `preflight.py` (or a
real `run_passes.py` session) treat the satellite as live immediately and
fail on a `.grc`/`.py` that doesn't exist yet. `toggle_satellite.py
--enable <name>` turns it on once the steps below are done. There's nothing
to say here about IQ toggling: once the `.grc` is wired (`wire_record_iq.py`,
or built from an already-wired template) that's detected automatically - see
"Toggling IQ recording" below. (`--record-iq-toggle` still works; it just writes
the explicit `record_iq_toggle: true` line.)

**If a record-only satellite's `.grc` would be identical to an existing one
except for name, NORAD, and frequency** (the common case when adding many
similar satellites), `new_record_only_satellite.py` does the whole thing -
generate the `.grc` from a template, add the entry, compile it - in one
step instead of three manual ones. See
[scripts-reference.md](scripts-reference.md) for how, and for exactly why
this is safe in a way the old, general-purpose `.grc` auto-generation
(described above) wasn't: a record-only flowgraph has none of the
structure (decoder, relay block, `kiss_encode_pdu`) that caused the
original bugs, so there's nothing for a templating tool to get wrong in
the same way. (With more than one radio - see [Stations](stations.md) - every
command on this page acts on the station you're in, each station keeps its own
`_record_only_template.grc` carrying that radio's device string (plus a
`_record_only_template_<band>.grc` for another SDR on the same antenna - the
nearest-frequency one is picked), and "next free
port" also avoids the other stations' ports.)

**extra_outputs** - for a satellite with a
second live output that a specific downstream app connects to, separate
from `relay.py`'s normal KISS path. Only two shapes are actually tracked
here, and which one applies depends entirely on which side is doing the
listening:

- **`tcp_client`** - the flowgraph connects out, same direction
  `relay.py` itself expects, just to a different destination than
  `SatsDecoder`. A genuine direct bypass, no extra tooling needed.
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

**`zeromq_pub` blocks are deliberately *not* tracked here at all** - a
`.grc` can have as many `zeromq_pub_msg_sink` blocks as it needs with
nothing in `satellites.yaml` referencing them. ZeroMQ PUB/SUB already
solves the decouple-short-lived-producer-from-long-lived-consumer
problem natively (a SUB socket connects, disconnects, and reconnects
independently at any time, and the publisher neither knows nor cares
whether anything's listening), so there's no listener-side
infrastructure to configure and nothing in this toolkit ever reads a
`zeromq_pub` address at runtime. Tracking it would be pure
documentation with zero functional payoff - worse, `preflight.py`
validated it with the same `[FAIL]`-level severity as a genuine
`tcp_bridge` mismatch, which actually *does* break a real pass, making
a harmless documentation drift look just as alarming as something that
would actually fail. If an existing `.grc` still has old `zeromq_pub`
entries in its `satellites.yaml` record from before this, they're
harmless leftovers, not bugs - `preflight.py` skips them silently
rather than flagging them, though there's no reason to keep them either;
[`edit_satellite.py --remove-extra-output`](scripts-reference.md) clears
one out.

`asrtussdv.grc` and `by704.grc` are the working examples - both run a
`network_socket_pdu` block as `TCP_SERVER` (an SSDV image viewer needs
to connect in). Each also has a `zeromq_pub_msg_sink` block for its
telemetry upload agent, but that block needs no entry here at all -
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
```
Multiple satellites feeding the *same* downstream app can share one
`bridge_port` - `tcp_bridge.py` runs one shared listener per distinct
port, with each satellite getting its own independent, self-retrying
upstream connection. Since only one satellite's flowgraph is normally
running at a time (one shared SDR - paired passes on two receivers are the
exception, so give two satellites that are recorded together their own
`bridge_port`s), only one of those upstream
connections is ever actually live in practice, so this is both safe and
genuinely more convenient: the downstream app never needs its connection
settings changed depending on which satellite is about to pass.
`ASRTU-1_SSDV` and `BY70-4` both do exactly this, sharing `bridge_port:
19985` for the same SSDV viewer.

`preflight.py` validates each entry against the real `.grc` the same way
it validates `producer_port` - confirming the named block exists and its
port actually matches, and - for `tcp_bridge` specifically - that
`bridge_port` is set, plus fleet-wide uniqueness across every
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
Use whichever NORAD number you find: SatNOGS sometimes lists a new satellite under a
temporary ID (98xxx) while its TLE carries the real number. `update_tle.py` accepts
either one in `satellites.yaml` and relabels the TLE to match, so `preflight.py`
finds it - no hand edit needed (see [scripts-reference.md](scripts-reference.md)).
A number of 100000 or more is fine too (the TLE spells it Alpha-5, `A0470` = 100470);
keep the plain number in `satellites.yaml`.

If the satellite is too new for `update_tle.py`'s sources (SatNOGS,
Celestrak) to have it at all - or you simply know better than they do - set
`custom_tle_file` in `satellites.yaml` to a file you maintain by hand with its
TLE. `update_tle.py` never writes to that file. A NORAD number listed there
**always uses your entry**, whatever its epoch and whatever the name line says;
only the catalog number on the TLE lines counts, and it has to match the
satellite's `norad` in `satellites.yaml`. Because it never ages out on its own,
`plan_passes.py`/`run_passes.py` print how old it is each time, and
`preflight.py` warns once it passes 14 days or the catalog has newer data -
delete the entry when you want to follow the catalog again. See
[scripts-reference.md](scripts-reference.md) for details.
```
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
Only touches the fields you actually pass, and only ever writes
`satellites.yaml` - never the `.grc` (`--record-iq-toggle` does open the
`.grc` to check it's wired before setting the flag, but only reads it).
If a change here (a new frequency, a corrected
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
field's type so it can hold an expression instead. If it's missing or the stock block is installed instead, a `.grc`'s
`recordOnStart` reads back as `False` after GRC opens it and `preflight.py` fails
the satellite - see "Install the gr-filerepeater fork" in [Setup](setup.md). Whether a satellite
supports the toggle is read from its `.grc` - wired means the Advanced File
Sink's Record On Start references `record_iq` and an enabled `record_iq`
Parameter block exists - so **there is nothing to declare in `satellites.yaml`**.
(It used to be a hand-maintained `record_iq_toggle: true` line, which every way of
adding a satellite had to remember; forgetting it showed up as a bare
`recordOnStart is True` preflight failure and a satellite that silently never
recorded IQ. An explicit `record_iq_toggle: true` or `false` still wins, if
present - `false` opts a satellite out.) The actual record-or-not decision is
normally a session-wide choice made when `run_passes.py` starts.

**Wire the `.grc`, recompile, and you're done.** The quick way:
```
python3 wire_record_iq.py BY70-4 JAMX-01     # shows a diff, asks, keeps a .bak
./regen_all.sh
```
By hand in GRC, the same thing is: add a Parameter block with ID
`record_iq`, type `int`, value `1`; set the Advanced File Sink's Record On
Start to `bool(record_iq)`; save and recompile. Either way that's all - the
toggle is detected from the `.grc`. Optionally, to write the setting down
explicitly, or to opt a wired satellite out:
```
python3 edit_satellite.py ASRTU-1_SSDV --record-iq-toggle
python3 edit_satellite.py ASRTU-1_SSDV --no-record-iq-toggle
```
`--record-iq-toggle` checks the `.grc` and refuses, changing nothing, if
it isn't wired - an explicit `true` is not a harmless note to self: set on a
satellite whose flowgraph doesn't accept `--record-iq`, every launch fails on
an argument error. `--no-record-iq-toggle` writes `record_iq_toggle: false`:
`run_passes.py` then never passes the flag, so recording follows the
`record_iq` parameter's own default in the `.grc` (`preflight.py` warns about
that case and says what the default is). (`preflight.py` checks the same wiring, and
`run_passes.py` re-verifies the compiled script at startup as a last
backstop; see [Troubleshooting](troubleshooting.md).)
See [run_passes.py](scripts-reference.md#run_passespy) for the
`--record-iq` flag this actually enables. That session-wide choice can
also be overridden for one specific upcoming pass, regardless of
satellite, via [toggle_pass_record_iq.py](scripts-reference.md) -
stored right in `schedule.yaml` against that one pass, taking priority
over `--record-iq` for it alone.

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

