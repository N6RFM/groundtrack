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
`notify: true` in `satellites.yaml` for desktop notifications via
`notify-send` (silently does nothing if `notify-send` isn't available,
e.g. on a headless box) - fires at AOS (once per pass, not again on a
retry) and LOS, and on every failed launch attempt: a flowgraph exiting
early (the message says which attempt of 3, and when it's giving up) or
an error caught during tracking - not just on success.

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
to pass when a new one gets added. With `radios.yaml` (multi-station mode -
see `station.py` below) it covers every station's satellites in one run and
ignores `--radio`: the stations share one TLE file, and this rebuilds that
whole file from whichever satellites it's given, so reading just one
station's list would drop the others' TLEs.

For a satellite too new for either source to have picked up at all (not
even a "temporary ID" entry yet), set `custom_tle_file` in
`satellites.yaml` to a file you maintain by hand - this script only ever
reads it (to avoid double-reporting a satellite as missing that it
already is), never writes to it. `--check-only` and the main fetch both
check it: a satellite covered there is never reported `MISSING`, and
during a real fetch, one not found in SatNOGS or Celestrak but present in
`custom_tle_file` is reported separately rather than failing the run.

**`custom_tle_file` is a stopgap, not a standing override.** `plan_passes.py`
and `run_passes.py` (which actually use the TLE data, not this script)
resolve a NORAD present in both files by comparing TLE epoch - whichever
entry's orbital data is genuinely more recent wins, regardless of which
file it's in. Leaving a satellite's entry in `custom_tle_file` after
SatNOGS or Celestrak catches up is harmless: `update_tle.py` keeps
`tle_file` current from then on, and since that entry's epoch keeps
advancing while the untouched custom one doesn't, `tle_file`'s copy
starts winning automatically, with a printed note explaining why.
Removing the stale custom entry at that point is just housekeeping, not
required for correctness.
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

The stray-compile check specifically isn't something you need to remember
to ask for, though: `preflight.py` runs it automatically, always
fixing (not just reporting) anything it finds, before its own other
checks - a stray left by a manual `grcc` run outside any tool here gets
caught the next time *anything* runs `preflight.py`, not only via
`doctor.py --fix`. `new_record_only_satellite.py` and the GUI's
"Regenerate .grc for selected" do the same immediately after their own
`grcc` calls, for the same reason: `-o` (which both now pass) only
redirects the main flowgraph's own output, not an embedded block's
companion file, so the sweep is the actual fix, not just a fallback for
when `-o` is missing.

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
`--record-iq-toggle` is the one flag that also *reads* the satellite's
`.grc` (never writes it): it checks that the `.grc` is genuinely wired for
the toggle - an Advanced File Sink whose Record On Start uses `record_iq`,
and an enabled `record_iq` Parameter block - and refuses, changing nothing,
if it isn't. Declaring the capability without the wiring isn't harmless: the
flag gets passed to a flowgraph that doesn't accept it, and every launch of
that satellite dies immediately. The checks match `preflight.py`'s, so the
two can't disagree. If it refuses because the `.grc` isn't wired, the next
tool is `wire_record_iq.py`, below.

## wire_record_iq.py

Wires a satellite's `.grc` for the per-run IQ recording toggle in one step -
the GRC work that has to exist before `record_iq_toggle` means anything. That
work is two things: a `record_iq` Parameter block (what makes the compiled
script accept a `--record-iq` option at all) and the Advanced File Sink's
Record On Start set to `bool(record_iq)` instead of a fixed `True`/`False`.
Accepts several names at once:
```
python3 wire_record_iq.py BY70-4 JAMX-01
python3 wire_record_iq.py BY70-4 --dry-run
python3 wire_record_iq.py BY70-4 --yes
./regen_all.sh
python3 edit_satellite.py BY70-4 --record-iq-toggle
```
Nothing changes about a satellite's behavior until something passes
`--record-iq`: the new parameter's default is taken from what Record On Start
was (`True` gives 1, `False` gives 0), so a flowgraph that always recorded
still always records. One of three narrow tools that write to a `.grc` (see
[Adding a satellite](adding-satellites.md)), and the most careful: it edits the
file as raw text - one line changed, one block inserted, nothing reformatted;
shows a diff and asks before writing (`--yes` skips the question, `--dry-run`
only shows the diff); keeps a `.bak` next to the file (never overwriting an
earlier one); re-checks its own result and restores the original if the check
fails; and does nothing if the flowgraph is already wired. It refuses, changing
nothing, when a human has to decide something: Record On Start is some other
expression than a plain `True`/`False`, a block named `record_iq` exists but
isn't a Parameter block or is disabled, there's no Advanced File Sink, or the
file isn't laid out the way GRC writes it. It does not recompile and does not
touch `satellites.yaml`. Its own output ends with the exact commands to run
next. If the flowgraph is open in GRC, close it without saving first and
reopen it afterwards - GRC doesn't notice a file changing on disk, and
saving from the stale window would silently undo the change (see "Known
caveats" in [Troubleshooting](troubleshooting.md)).

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

## new_record_only_satellite.py

Generates a record-only satellite's `.grc` from a template, adds it to
`satellites.yaml` (`add_satellite.py --record-only`), and compiles it -
one command instead of building the `.grc` by hand, running
`add_satellite.py`, and running `grcc` separately. Worth it specifically
when a batch of satellites' `.grc` files would be identical except for
name, frequency, and NORAD (not present in the `.grc` at all - it's a
`satellites.yaml`-only, TLE-lookup concept):
```
cp flowgraphs/scionx.grc flowgraphs/_record_only_template.grc   # once
python3 new_record_only_satellite.py --name NEWSAT-7 --norad 12345 --freq 437500000
python3 new_record_only_satellite.py --name NEWSAT-7 --norad 12345 --freq 437500000 --dry-run
```
Uses `flowgraphs/_record_only_template.grc` if it exists, else
`flowgraphs/scionx.grc` directly (with a suggestion to save a dedicated
copy - editing `scionx.grc` later for its own reasons would otherwise
silently change what every future satellite is built from). Six fields
change: the flowgraph's internal id/title, the recording filename prefix,
the waterfall's display name, and the frequency (written once, read by
both `freq` and `nfreq`, which must match in the template - the normal
state for one sitting at its own downlink frequency - or the tool refuses
rather than guess which should change). Nothing else - every block,
connection, and other parameter is copied from the template exactly as
`vet_grc.py --fix` and `wire_record_iq.py` already do, `--yes`/`--dry-run`
work the same way, and it never overwrites an existing satellite's `.grc`.

This is a narrower, more deliberate version of the auto-generation
`add_satellite.py` used to do and stopped doing - see "Why nothing here
touches `.grc` files" in [Adding a satellite](adding-satellites.md) for
that history, and for why a record-only flowgraph's much simpler, fixed
shape (no decoder, no relay block, no `kiss_encode_pdu`) means the
specific bugs that happened there don't apply here. `--record-iq-toggle`
passes through to `add_satellite.py`; if the template isn't itself wired
for the toggle, the tool says so and names `wire_record_iq.py` as the
next step.

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
`yes`) is the session-wide default, affecting only satellites whose `.grc` is wired for
the toggle (detected automatically; an explicit `record_iq_toggle: true`/`false`
in `satellites.yaml` overrides) - see
[Adding a satellite](adding-satellites.md)
for what that requires. Every other satellite launches exactly as
before, regardless of this flag. A pass can override this session
default individually - see `toggle_pass_record_iq.py` below - which
takes priority over `--record-iq` for that one pass only. Before the run
starts, `run_passes.py` asks each toggle-capable satellite that has a queued pass
whether its compiled script actually accepts
`--record-iq` (via the script's own `--help`, which exits before touching any
hardware); if not, it prints a warning and launches that satellite without
the flag rather than crashing it every pass. A flowgraph that exits early at
launch is retried at most 3 times, 15 seconds apart, and then given up on for
that pass - the rotor is only pre-positioned toward the next pass once it has
stopped trying. A pass that ends normally (including by the elevation safety
net, which can fire before the scheduled LOS) is never relaunched. Every real
pass's actual outcome (started/completed/crashed/error, and which
`record_iq` value was actually used) is appended to `pass_log.jsonl` -
see `show_pass_log.py` below. `notify: true` in `satellites.yaml` also
triggers a desktop notification on each failed launch attempt, not just
on AOS/LOS.

## toggle_pass_record_iq.py

Toggles IQ recording on or off for specific upcoming passes in the
queue - a per-pass-instance override, independent of which satellite it
is, distinct from both the per-satellite capability (read from the
`.grc`) and `run_passes.py`'s own `--record-iq` (the session-wide
default). Takes priority over the session default for that one pass
only, when set - unset, that pass just falls back to whatever
`--record-iq` says:
```
python3 toggle_pass_record_iq.py
```
Interactive: lists every upcoming approved pass with its current
override state and whether the satellite is even capable of being
toggled at all, then prompts for which pass number(s) to change and
whether to turn recording on, off, or clear back to no override. Skips
(with a clear explanation) any selected pass whose satellite isn't
toggle-capable, since a per-pass override would have no
effect there. Only ever writes to `schedule.yaml`, never `satellites.yaml`
or any `.grc`. `show_queue.py`'s own listing shows each pass's current
`record_iq` state (`ON`/`OFF`/`(default)`/`n/a`) for a quick look without
needing to run this interactively.

## show_pass_log.py

Human-readable summary of `pass_log.jsonl` - `run_passes.py`'s
persistent, append-only record of what actually happened during each
real pass, as opposed to `schedule.yaml`'s record of what was predicted
or approved. One JSON object per line: a `started` record for each
launch attempt (with its `attempt` number and the `record_iq` value
actually used), then how that attempt ended - `crashed` (exit code,
`attempt`, and `will_retry`), `error`, or, for a pass that ran to its end,
`completed` (with why: the scheduled LOS, or the elevation safety net).
This tool correlates them by satellite and AOS time into one row per real
pass, showing how it finally ended:
```
python3 show_pass_log.py
python3 show_pass_log.py --last 20
python3 show_pass_log.py --failures-only
```
A pass that needed more than one launch attempt shows the count (e.g.
`3 launch attempts`). A pass with a `started` record but no matching
outcome is called out explicitly rather than silently dropped - it usually
means `run_passes.py` itself was killed mid-pass, not just the flowgraph
crashing (which would have logged its own `crashed` outcome).
`pass_log.jsonl` is local run data and is gitignored.

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

## station.py and radios.yaml

For a ground station with more than one SDR/antenna system running
independently - say a beam-steered receiver and a fixed helix one, often at
the same time. Each system is a **station**: a complete folder of its own
(`satellites.yaml`, `flowgraphs/`, `schedule.yaml`, `pass_log.jsonl`, and
`run_passes.lock` while it's tracking), while the scripts themselves live
once, in the project root, and are never duplicated. Every script acts on
one station at a time, and two copies of `run_passes.py` - one per station -
run side by side sharing nothing but the TLE file.

`radios.yaml` (copy `radios.example.yaml`) lists the stations and which one
the GUI opens on. A script picks its station, changes into that folder, and
from then on runs exactly as it always has - every relative path it already
used just resolves inside the station. Choosing one:
```
python3 run_passes.py --radio mini
GROUNDTRACK_STATION=mini python3 run_passes.py
python3 run_passes.py          # at a terminal: asks which station
```
`--radio` wins over the environment variable, which wins over being asked;
there is deliberately never a silent default, since starting the wrong
radio's tracking is worth one extra keystroke. With no terminal and no
choice made, a script refuses and says how to choose. `--help` works without
choosing. With no `radios.yaml` at all, nothing changes: the classic
single-folder layout works exactly as before.

What differs between radios lives in that station's own files, not in
`radios.yaml`: `rig_port`, `rot_host`/`rot_port` (leave those two out and
the rotor is never touched for that station - the same existing mechanism
as running with no rotor at all), and the SDR device string inside each of
its `.grc` files. For record-only satellites, keep a
`_record_only_template.grc` in each station's `flowgraphs/` carrying that
radio's device string, and `new_record_only_satellite.py --radio ...` uses
the right one automatically.

The one thing stations share is the TLE file: point every station's
`tle_file` (and `custom_tle_file`, if used) at the same path, e.g.
`../tle/amateur.txt`. `update_tle.py` is the one script that spans stations
- it refuses, rather than guess, if two stations name different TLE files.

`python3 station.py --list` shows what's configured; `--shell` prints the
`export`/`cd` lines `regen_all.sh` evals. The GUI switches station while
running (`station.switch()`) instead of choosing once at start-up, and
`preflight.py` inside a station also runs `station.cross_station_conflicts()`:
`rig_port`, rotor and relay/bridge ports must not be shared with another
station. Automatic "next free port" (`add_satellite.py`, the GUI's Add
dialog, `plan_passes.py --add-satellite`) likewise avoids every other
station's ports.

## migrate_to_stations.py

The one-time move from the classic single-folder layout to multi-station mode
(see [Stations](stations.md) for the whole picture): your current fleet
becomes the first station, a new empty one is created beside it, and
`radios.yaml` is written.
```
python3 migrate_to_stations.py --second-rig-port 4534 --dry-run
python3 migrate_to_stations.py --second-rig-port 4534 \
    --first-label "R2 + beam (Az/El)" --second-label "Mini + helix (fixed)" \
    --second-template ~/my_working_mini_flowgraph.grc
```
`flowgraphs/` moves with `git mv` (history follows the files; compiled and
untracked files travel too), `satellites.yaml`/`schedule.yaml`/
`pass_log.jsonl` move alongside, and the only edit inside a moved file is
prefixing `../` to a relative `tle_file`/`custom_tle_file`, done as a plain
line edit so comments and quote style survive. Every precondition is checked
first and all problems reported at once: a live `run_passes.py`, compiled
flowgraphs stranded in the project root (`doctor.py --fix` first), a station
folder that already exists, a `rig_port` clash, an unusable template. The
files git doesn't track are copied to `.pre_stations_backup/` first, and if
any step fails the ones already done are undone. `--dry-run` changes nothing.

`--second-template` installs a flowgraph you already run on the second radio
as its `_record_only_template.grc`, after proving `new_record_only_satellite.py`
can actually generate from it. It's deliberately not derived from the first
station's flowgraph - the radios differ in more than a device string.
