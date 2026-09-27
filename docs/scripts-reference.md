# groundtrack: Scripts Reference

[← back to README](../README.md)

## What each script does

**`setup_station.py`** - one-time/occasional wizard for ground-station-level
settings (lat/lon/alt, TLE source, `rig_port`). Leaves your satellite list
untouched. Add a new satellite to the fleet with:
```
python3 plan_passes.py --add-satellite
```
(only adds the config entry - you still need to create its `flowgraphs/<name>.grc`).

**`plan_passes.py`** - predicts every pass for every configured satellite
over a lookahead window (`--hours`, default 24) using Skyfield, and writes
them to `schedule.yaml` with an `approved:` flag per pass:
```
python3 plan_passes.py --hours 24                # list + approve all by default
python3 plan_passes.py --hours 48 --interactive   # y/n prompt per pass
```
Or hand-edit `schedule.yaml` afterward, setting `approved: false` on
anything you don't want recorded. Nothing is ever recorded on a pass that
isn't in this approved list.

Note: if two satellites' approved passes overlap, only one can actually
record (single SDR, no pre-emption). `plan_passes.py` detects this
automatically after you approve/reject: with `--interactive` it prompts
you to actively choose which one to keep (or both, or neither); without
it, it prints a clear warning listing every conflict rather than staying
silent, and leaves the existing first-started-wins behavior in place.

**`run_passes.py`** - the executor. Reads `schedule.yaml`, and for each
approved pass: waits for wall-clock AOS, launches that satellite's
flowgraph, feeds it Doppler-corrected frequency and (if configured) points
the rotor, and stops the flowgraph at the scheduled LOS (or earlier, if
live elevation drops below `min_elev_deg` first - a safety net for TLE
drift since planning). Run with `--verbose` to print every update:
```
python3 run_passes.py --verbose
```
Refuses to start a second instance against the same folder (a
`run_passes.lock` file, checked against the running PID - a stale lock
left over from a crash is detected and cleared automatically). Set
`notify: true` in `satellites.yaml` for desktop notifications at AOS/LOS
via `notify-send` (silently does nothing if `notify-send` isn't available,
e.g. on a headless box).

**`relay.py`** - persistent process, independent of any pass, that lets
your downstream KISS decoder hold one stable connection across every
AOS/LOS cycle instead of reconnecting every pass. Enables TCP keepalive
(`SO_KEEPALIVE`) on every consumer connection so a genuinely dead link
gets detected and cleaned up rather than left as a zombie. See "The
relay" below.

**`doctor.py`** - one command to check the environment (right folder,
stray processes, occupied ports) plus everything `preflight.py` checks.
See "doctor.py" above.

**`preflight.py`** - static config checks (freq/port/decoder-file
correctness) and an optional `--live` mode that briefly launches each
flowgraph for real. See "Preflight checks" above.

**`test_downstream.py`** - pushes frames through `relay.py` to your
connected decoder(s) for every satellite, automatically using real
captured frames if any exist yet or synthetic ones if not. See "Sending
test frames" above.

**`show_queue.py`** - reads `schedule.yaml` and prints the approved pass
queue (satellite, AOS, LOS, duration, max elevation), independent of
whether `run_passes.py` is running. See "Checking the pass queue" above.

**`toggle_satellite.py`** - enable or disable a satellite without
deleting its config:
```
python3 toggle_satellite.py --list
python3 toggle_satellite.py --disable GEOSCAN-1
python3 toggle_satellite.py --enable SCIONX
```
A disabled satellite is skipped everywhere - `relay.py` won't bind its
ports, `run_passes.py`/`plan_passes.py` won't schedule or execute passes
for it, `preflight.py` reports it as skipped rather than checking it.
Its full entry stays in `satellites.yaml` untouched, so re-enabling it
later needs no reconfiguration at all. See "Adding a satellite" below.

**`update_tle.py`** - refreshes the TLE file, one bulk fetch from SatNOGS
covering every configured satellite (including "temporary ID" satellites
too new for Celestrak/Space-Track's official catalog yet), with
Celestrak consulted per-satellite only as a fallback for whatever
SatNOGS didn't have:
```
python3 update_tle.py
python3 update_tle.py --check-only    # report coverage/age, don't download
```
Nothing to specify per-satellite - every satellite in `satellites.yaml`
is covered automatically by both sources, so there's no flag to remember
to pass when a new one gets added.
Validates the download before overwriting the real file, and reports
exactly which configured satellites are missing afterward rather than
failing silently later inside `plan_passes.py`.

**`vet_grc.py`** - vets a `.grc` for the specific bugs this project has
actually hit, all stemming from hand-editing in GRC (especially
copy/paste, which GRC handles by silently appending `_0`, `_0_0`, etc.
to avoid a name collision, without warning you it happened):
```
python3 vet_grc.py                          # every flowgraphs/*.grc
python3 vet_grc.py flowgraphs/asrtussdv.grc  # one specific file
python3 vet_grc.py --fix flowgraphs/asrtussdv.grc
```
Checks for: a block type that should only ever appear once existing
twice; a block carrying GRC's double-suffix copy/paste signature even
when only one instance survives; `gpredict_doppler` present anywhere
(always wrong for this toolkit, see below); `rig_freq_poller` missing
when live Doppler tracking is expected; `osmosdr_source` re-tuning real
hardware instead of holding a fixed frequency; `options.id` not matching
the filename; a leftover Qt waterfall. `--fix` only ever applies to the
first two - pure renames, nothing structural - and always shows a diff
before writing plus keeps a `.bak` of the original; everything else
still means opening GRC. See [Adding a satellite](adding-satellites.md) for why this stays this narrowly scoped.

**`tcp_bridge.py`** - `relay.py`'s mirror-image, for a satellite whose
flowgraph runs its own `TCP_SERVER` instead of connecting out as a
client (see [The tcp_bridge](relay-and-bridging.md) for the full picture):
```
python3 tcp_bridge.py --verbose
```
Same ownership rules as `relay.py` - standalone, never auto-started,
start once and leave running for a session. Skips disabled satellites.
Multiple satellites sharing one downstream app can share one
`bridge_port`; each still gets its own independent, self-retrying
upstream connection.

## doctor.py - one command to check the environment

Run this first, any time something feels off, or as a habit before a
session:
```
python3 doctor.py
```
It checks, in order: which folder you're actually running from (and flags
if it's inside Trash - a real issue we hit once), whether duplicate copies
of this fleet folder exist elsewhere on disk, which fleet-related
processes are currently running and from where, which of the fleet's
ports are free vs. already occupied (and by what), whether any compiled
flowgraph files have landed at the repo root instead of `flowgraphs/` -
`grcc` always writes its output to the current directory, ignoring the
`.grc`'s own folder, for both the main flowgraph and a separate companion
file per embedded Python block - and a live snapshot of what's actually
installed (Python, OS, GNU Radio, gr-satellites, pyyaml, skyfield,
Hamlib) - see [Environment](environment.md) for what's actually confirmed
working, since none of these are checked against a required minimum
here. It then runs `preflight.py`'s full config checks automatically -
so `doctor.py` is a strict superset of `preflight.py`; you can run
either, but `doctor.py` catches a wider class of problems (like an
orphaned process from a since-deleted folder silently holding a port,
which `preflight.py` alone has no way to see).

```
python3 doctor.py --fix
```
Actually removes/moves the stray compiled files it finds - deleting
disposable embedded-block companions, and either deleting a stray main
flowgraph `.py` (if the correct copy already exists in `flowgraphs/`) or
moving it into place (if it doesn't). Safe regardless: nothing else in
this toolkit ever reads these files from the repo root, so cleaning them
up can't break anything that was working. `--fix` is stripped out before
anything else is passed through to `preflight.py`, so it won't cause an
"unrecognized arguments" error there.

Anything else after `doctor.py` on the command line is passed straight
through to `preflight.py`, so `python3 doctor.py --live` works too.

For a quick glance instead of the full report - TLE age, whether
rigctld/rotctld/relay.py are up, and time until the next approved pass:
```
python3 doctor.py --status
```

## edit_satellite.py

Updates fields on an already-configured satellite - see
[Adding a satellite](adding-satellites.md) for the
full picture, including `extra_outputs` and `record_iq_toggle`. Only
ever touches `satellites.yaml`, never the `.grc`, and only touches the
specific fields you actually pass:
```
python3 edit_satellite.py GEOSCAN-1 --freq 435970000
python3 edit_satellite.py GEOSCAN-1 --enabled
python3 edit_satellite.py ASRTU-1_SSDV --record-iq-toggle
```

## suggest_extra_outputs.py

Scans a satellite's real `.grc` for `network_socket_pdu` blocks not yet
claimed by an `extra_outputs` entry, and drives `edit_satellite.py` to
add them - using the block's actual name and port read straight from
the file, never hand-typed. Correctly excludes the satellite's primary
relay connection (`producer_port`), if it has one, since that's not an
"extra" output. `zeromq_pub_msg_sink` blocks are never suggested here -
see [Adding a satellite](adding-satellites.md) for why they don't need
tracking in `satellites.yaml` at all. The only things this ever asks
for are a name and, for `tcp_bridge`, a `bridge_port` - the two things
that genuinely can't be read from the `.grc` itself:
```
python3 suggest_extra_outputs.py ASRTU-1_HYBRID
python3 suggest_extra_outputs.py ASRTU-1_HYBRID --dry-run   # print the
    # edit_satellite.py commands it would run, without running them
```
Run this after building or editing a `.grc` with a new network-facing
block, before hand-writing any `edit_satellite.py --extra-output-*`
command - every `extra_outputs` bug found in one real session (a typo'd
address, a block name that didn't match, two entries sharing one name,
a port that didn't match the `.grc`) came from transcribing these
values by hand, which this tool exists specifically to eliminate.

## plan_passes.py

Computes upcoming passes for every enabled satellite from their TLEs,
shows them for review, and writes the approved ones to `schedule.yaml` -
the step between having satellites configured and `run_passes.py` having
anything to actually execute:
```
python3 plan_passes.py
python3 plan_passes.py --hours 48
python3 plan_passes.py --interactive   # prompt y/n per pass instead of approving all
python3 plan_passes.py --add-satellite # interactively append a new satellite, then exit
```
Each pass's listing includes its peak angular rate (`peak deg/s`) - the
fastest the rotor would need to track during that specific pass, sampled
every 2 seconds from AOS to LOS. If `rot_max_deg_per_sec` is set in
`satellites.yaml` (see [Tracking Control](tracking-control.md) for how to
measure it), any pass whose peak rate would exceed it is flagged
`!! EXCEEDS rotor max` right in the listing, before you approve it - real,
physical hardware limits surfaced as advance information instead of
something noticed mid-pass. A flagged pass isn't blocked; it's still your
call whether to approve it.

## run_passes.py

The actual execution engine - waits for each approved pass in
`schedule.yaml`, launches that satellite's flowgraph at AOS, retunes for
Doppler via `rigctld`, steers the rotor via `rotctld`, and stops the
flowgraph at LOS. Runs indefinitely; Ctrl-C to stop.
```
python3 run_passes.py --verbose
python3 run_passes.py --verbose --status-interval 10
python3 run_passes.py --no-preposition
python3 run_passes.py --record-iq no
```
`--verbose` prints a live el/az/freq/Doppler line while a pass is
active - `--status-interval` (default `5`) controls how often that line
actually redraws; Doppler correction itself still recomputes every
second regardless, but the rotor only gets a new command when the
satellite has actually drifted enough to warrant one (lead-ahead
targeting - see [Tracking Control](tracking-control.md) - not a fixed cadence).
Only the printed line is throttled by this flag, since a
real terminal session copied to a log file can otherwise look like a
flood of scrolling lines even though only one line was ever changing in
place. By default, when a pass ends (however it ends - normal LOS, an
elevation safety-net, or the flowgraph crashing early), the rotor is
pre-positioned toward wherever the *next* approved pass will actually
rise, rather than left wherever the finished pass happened to end -
`--no-preposition` disables this. `--record-iq {yes,no}` (default
`yes`) is a session-wide choice affecting only satellites with
`record_iq_toggle: true` set - see
[Adding a satellite](adding-satellites.md)
for what that requires. Every other satellite launches exactly as
before, regardless of this flag.

## test_rotor_throttle.py

**Historical, superseded - kept for reference, not for verifying current
behavior.** This was the standalone test for the very first rotor
throttle design (`min_move_deg`/`min_interval_s` on `Rotctld.point()`
directly), before that design was replaced entirely by the lead-ahead
targeting `test_rotor_leadahead.py` (below) actually tests. It has its
own *copy* of the old `Rotctld` class, not an import from
`run_passes.py`, so running it today exercises code that no longer
matches what's actually deployed at all - `Rotctld.point()` in the real
`run_passes.py` no longer has `min_move_deg`/`min_interval_s` parameters
to test. Use `test_rotor_leadahead.py` for anything about actual, current
rotor behavior; this one only remains useful if you want to see how the
design's very first iteration behaved.

## test_rotor_leadahead.py

Standalone verification for the lead-ahead rotor logic - imports
`Rotctld`, `find_lead_ahead_target`, and `maybe_update_rotor` directly
from `run_passes.py` by file path (not a reimplementation), so it tests
the actual deployed code. Drives your real, already-running `rotctld`
with synthetic, constant-velocity motion (not a real TLE), so expected
behavior can be worked out by hand rather than depending on whatever a
real satellite happens to be doing right now:
```
python3 test_rotor_leadahead.py
python3 test_rotor_leadahead.py --host 127.0.0.1 --port 4533
```
This will physically move the antenna, same as a real pass. Runs a fast
slew and a slow slew, each printing every actual `SENT` command (with
whether it was a genuine lead-ahead target ahead of the current
position) plus an independent `rotctld` position query every 10
seconds - useful for watching the rotor's own physical digital display
against what the script separately reads back, to build real confidence
the two agree. No pass, schedule, or TLE needed - useful any time, not
just while waiting for a real satellite.

## measure_rotor_speed.py

Empirically measures your rotor's actual max slew rate - a clean,
isolated point-to-point timed move (no lead-ahead/threshold tracking
logic involved at all), the real basis `rot_max_deg_per_sec` should come
from rather than a guess or a spec sheet number. See [Tracking
Control](tracking-control.md) for the full rationale, including a real,
hard-learned lesson about why the `--tolerance` setting matters more than
it might seem:
```
python3 measure_rotor_speed.py
python3 measure_rotor_speed.py --tolerance 2.0
python3 measure_rotor_speed.py --distances 20 60 120
```
Tests several move distances and suggests a `rot_max_deg_per_sec` value
with a safety margin already applied, based on the *slowest* measured
rate - a real pass involves sustained tracking much more like a long
test move than a short burst, so the slowest result is the more
representative, trustworthy basis for the suggestion. This WILL
physically move the antenna.

## ci_check.py

The subset of `preflight.py`'s checks that can actually run on a bare CI
runner - no GNU Radio, no Hamlib, no real TLE file needed. Checks: every
`.py` file compiles (catches a genuine syntax error before anyone runs
anything), `satellites.example.yaml` parses and each entry's basics are
present, its `freq`/`nfreq` match the corresponding `.grc`,
`network_socket_pdu` hasn't reverted to `TCP_SERVER` where a client
connection was intended, and no port collisions across the fleet. Exists
because these are exactly the classes of bug that have actually shipped
before - catching them here means before a PR merges, not after someone
runs `preflight.py` on real hardware and wonders why:
```
python3 ci_check.py
```

## locate_decoders.py

Searches a directory tree for a satellite's decoder `.yml` (matched by
NORAD ID or name appearing in the filename) and patches the matching
`satellites_satellite_decoder` block's `file` parameter in that
satellite's `.grc` directly - see [Adding a
Satellite](adding-satellites.md) for why this is one of only two
narrow, deliberate exceptions to "nothing here touches `.grc` files":
```
python3 locate_decoders.py ~/Launches
python3 locate_decoders.py ~/Launches --dry-run
```
Skips ambiguous cases (more than one plausible match) rather than
guessing, printing every candidate so you can patch that one by hand
instead. Worth knowing before running it for real: this round-trips the
whole `.grc` through PyYAML (load, modify, dump), unlike `vet_grc.py
--fix`'s careful raw-text editing - it will reformat the file's
structure as a side effect, so review the diff before committing, same
as any other `.grc` change. Remember to `grcc` the `.grc` afterward, same
as any other manual edit - this only ever touches the file on disk, not
the compiled `.py`.

## send_test_frames.py

Sends real KISS frames through `relay.py` to your downstream decoder
without waiting for an actual pass - useful for confirming the whole
decode pipeline works before ever pointing an antenna at anything.
Connects to the satellite's `producer_port` as if it were the flowgraph
itself, so it exercises the exact same path a real pass uses; `relay.py`
must already be running. Two modes:
```
python3 send_test_frames.py --satellite GEOSCAN-2 --replay geoscan2.kss
python3 send_test_frames.py --satellite GEOSCAN-2 --synthetic 5
```
`--replay` sends real frames captured during a past pass (from
`satellites_kiss_file_sink_0`'s own output file) - these decode
meaningfully, since it's genuine data. `--synthetic N` sends `N`
garbage-payload frames purely to prove the relay/decoder connection and
KISS framing work at all; your decoder will likely flag CRC/parse errors
on these, which is expected - the point is confirming frames arrive,
not that they mean anything. `--count`/`--delay` control how many frames
and how far apart, for either mode.
