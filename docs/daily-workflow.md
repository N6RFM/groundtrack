# groundtrack: Daily Workflow

[← back to README](../README.md)

> **More than one radio?** Everything below acts on one station at a time -
> `--radio NAME` on any command, the GUI's station switch, or you'll be asked.
> See [Stations](stations.md).

## Preflight checks (run this before every session)

`preflight.py` catches exactly the class of bugs we've hit already - typos
in `satellites.yaml`, freq/nfreq mismatches against a `.grc`,
`network_socket_pdu` silently reverting to `TCP_SERVER`, missing decoder
files, uncompiled/stale `.py` files, port collisions, satellites missing
from the TLE file - all without waiting for a real pass to expose them.

```
python3 preflight.py
```

Run this after any edit to `satellites.yaml` or any `.grc`, and as a habit
before starting `run_passes.py` for a session. It exits non-zero if
anything failed, so it's also cron/CI-friendly if you want to wire it into
your daily TLE-refresh routine.

`run_passes.py` does **not** run these checks for you. One overnight run
launched a misconfigured satellite's flowgraph - which died instantly -
hundreds of times before anyone noticed. `run_passes.py` now caps launch
retries at 3 and re-verifies at startup the one setting that caused that
(that a flowgraph really accepts `--record-iq` when its `.grc` is wired for
the toggle), but this step is still what catches config problems before a session
instead of during one.

For a deeper check that actually launches each flowgraph briefly (using
real SDR hardware) to confirm it starts without crashing and correctly
connects out to the relay:
```
python3 preflight.py --live                    # tests every satellite, ~10s each
python3 preflight.py --live --only GEOSCAN-2    # just one
```
This stands in for `relay.py` temporarily (so it doesn't need to already
be running) and just checks the connection arrives and the process
doesn't crash - it doesn't require a real satellite to be overhead, since
it's testing the pipeline's wiring, not decoding a real signal.

## Daily workflow

```
# 1. refresh TLEs (cron this)
python3 update_tle.py

# 2. sanity-check everything before touching hardware/schedules
python3 doctor.py

# 3. plan: predict upcoming passes and approve/reject them
python3 plan_passes.py --hours 24 --interactive

# 3b. (optional) turn IQ recording on or off for specific queued passes
python3 toggle_pass_record_iq.py

# 4. start the persistent relay (leave running - only needs restarting if it dies;
#    only needed if at least one configured satellite uses one)
nohup python3 relay.py > relay.log 2>&1 &

# 5. start your antenna rotor daemon, pointed at your real hardware
rotctld -m 607 -r /dev/ttyUSB2 &

# 6. connect a decoder tab/instance for each decode-and-relay satellite,
#    at its own consumer_port, and leave them open - see The relay in relay-and-bridging.md for why this matters and how to check it's actually done
#    correctly. Recording-only satellites need nothing here.

# 7. execute: waits for AOS, launches flowgraphs, drives Doppler + rotor
python3 run_passes.py --verbose

# 8. afterwards: what actually happened, one row per pass
python3 show_pass_log.py
```

Steps 3-6 only need to be started once per session (they're long-running);
step 1-2 is the thing to repeat daily as TLEs update. Redirecting relay.py's
output to a log file (rather than `python3 relay.py &` directly) keeps your
terminal prompt clean instead of its startup messages interleaving with it -
check on it anytime with `tail -f relay.log`.

### Checking the pass queue

`run_passes.py` doesn't have a `--status` or `--list` flag; it just prints
the next pass as it runs. To see the full approved queue at any time -
whether or not `run_passes.py` is currently running:
```
python3 show_queue.py
```
This reads `schedule.yaml` directly and prints every `approved` pass,
sorted by AOS, with a status column (`past` / `ACTIVE` / `NEXT` /
`upcoming`), duration, max elevation, and a `RECORD_IQ` column: `ON` or
`OFF` if that pass has a per-pass override (set with
`toggle_pass_record_iq.py`), `(default)` if it will follow the session-wide
`--record-iq`, or `n/a` if that satellite isn't toggle-capable (its `.grc` isn't wired for it).

To also see unapproved/rejected passes (e.g. ones `plan_passes.py`'s
overlap resolution dropped):
```
python3 show_queue.py --all
```
`schedule.yaml`'s pass fields as of this writing: `name`, `norad`, `aos`,
`los`, `max_elevation_deg`, `peak_rate_deg_per_sec`, `approved`, and
optionally `record_iq` (the per-pass override). If `plan_passes.py`'s schema ever
changes, update the key lists near the top of `show_queue.py` to match.

### Starting from an uncertain state

The sequence above assumes a clean slate. If you're not sure what's still
running from an earlier session - a previous `relay.py` you forgot about,
a stale `rotctld`, a leftover debugging process - don't guess and don't
just try to start everything again on top of it. Kill it all, verify it's
actually gone, then rebuild step by step with a checkpoint after each one.
This is slower than the Daily workflow above, on purpose - it's for
recovering from confusion, not for a normal day.

**0. Kill everything:**
```
pkill -f relay.py
pkill -f run_passes.py
pkill -f SatsDecoder
pkill -f "flowgraphs/"    # every satellite's compiled flowgraph, whatever they're named
pkill -f rotctld
pkill -f rigctld
```
"No process found" for any of these is fine - it just means that one
wasn't running.

**Verify it's actually clean before rebuilding anything:**
```
python3 doctor.py
```
Check two things specifically: "Fleet-related processes currently
running" should say `none found`, and every port doctor.py lists for
your currently-enabled satellites should say `free`. Don't move on
until both are true - if anything still shows up, that's a process the
kill list above didn't catch, and it needs its own `kill <PID>` before
continuing.

**1-2. Refresh TLEs, then sanity-check config:**
```
python3 update_tle.py
python3 doctor.py
```
Confirm `update_tle.py` reports every configured satellite covered, with
no `MISSING from TLE file` lines (a satellite covered only by
`custom_tle_file`, if you've set one, counts as covered too). Confirm `doctor.py` ends in `0 failed` -
if `.grc` files were touched since the last session (GNU Radio Companion
re-saving them counts), you may see a "`.py` is up to date with `.grc`"
failure here; fix with `./regen_all.sh` before continuing.

**3. Plan passes:**
```
python3 plan_passes.py --hours 24 --interactive
```
Before approving, you'll get a chance to actively resolve any overlaps
between different satellites - see the note under `plan_passes.py` below.
This also fully regenerates `schedule.yaml`, so any satellite you've
since disabled with `toggle_satellite.py` simply won't have entries in
it anymore - nothing to prune by hand.

**4. Start the relay** (skip this step entirely if every currently-enabled
satellite is recording-only), **then confirm it before moving on:**
```
nohup python3 relay.py > relay.log 2>&1 &
cat relay.log
```
If `cat` shows nothing, wait a second and run it again - backgrounding
sometimes returns your prompt before the startup banner prints. You're
looking for a `producer :.../consumer :... (TCP keepalive on)` line for
each satellite that uses the relay, and no `OSError: ... address already
in use` traceback. If you see that traceback, something from step 0
wasn't actually killed - go back to `doctor.py`, don't just retry the
same command.

**5. Start the rotor, then confirm it before moving on:**
```
rotctld -m 607 -r /dev/ttyUSB1   # match your actual device
python3 doctor.py
```
Check the antenna rotor port line specifically (`rot_port` in
`satellites.yaml`, default 4533) - it should show `IN USE` with your
`rotctld` PID, not `free`.

**6. Connect a decoder tab for each decode-and-relay satellite, then
confirm from the relay's side:**
Point each tab at `127.0.0.1` and that satellite's own `consumer_port`,
then check:
```
tail -20 relay.log
```
You want one `consumer connected` line per satellite that uses the
relay - not fewer, and not the same satellite twice while another is
missing.

**7. Execute:**
```
python3 run_passes.py --verbose
```
On startup it should print the number of approved passes loaded and
identify the next one by name and AOS time. If a pass should be starting
soon and nothing happens, that means it's still counting down - it won't
launch a flowgraph until wall-clock AOS actually arrives.

**`add_satellite.py`** - adds a new satellite's config entry to
`satellites.yaml`:
```
python3 add_satellite.py --name GEOSCAN-3 --norad 64893 --freq 435742000
```
For a satellite with no decoder yet, or one whose outputs will connect
directly via `extra_outputs` rather than through the relay:
```
python3 add_satellite.py --name SCIONX --norad 69880 --freq 437500000 --record-only
```
Auto-assigns the next free `producer_port`/`consumer_port` (unless
`--record-only`, or overridden with `--producer-port`/`--consumer-port`),
refuses a NORAD or port collision with another configured satellite, and
tells you exactly what's still needed afterward. Always adds the
satellite as `enabled: false` (nothing is built yet);
`--record-iq-toggle` also writes an explicit `record_iq_toggle: true` now
(optional: wiring the `.grc` later with `wire_record_iq.py` is what enables the
toggle - it's detected from the `.grc`). Otherwise only
ever touches `satellites.yaml` - never generates or modifies a `.grc`.
Building `flowgraphs/<name>.grc` is a manual step in GRC every time, same
as `edit_satellite.py` below and every other `.grc`-shaped thing in this
toolkit - see "Why nothing here touches `.grc` files" under "Adding a
satellite" for why.

`plan_passes.py --add-satellite` does roughly the same job through an
interactive prompt instead of flags (including the same `enabled: false`
default and a y/n prompt for `record_iq_toggle`), though without
`add_satellite.py`'s duplicate-NORAD check. For a batch of record-only
satellites whose `.grc` files would otherwise be identical except for
name/frequency, `new_record_only_satellite.py` (see
[scripts-reference.md](scripts-reference.md)) builds the `.grc`, adds the
entry, and compiles it in one step.

**`edit_satellite.py`** - updates an already-configured satellite's
fields, and its `extra_outputs` list, without touching anything you
don't explicitly pass:
```
python3 edit_satellite.py GEOSCAN-1 --freq 435970000
python3 edit_satellite.py GEOSCAN-1 --enabled
python3 edit_satellite.py ASRTU-1_SSDV --extra-output-name ssdv_viewer \
    --extra-output-protocol tcp_bridge --extra-output-block network_socket_pdu_0 \
    --extra-output-port 9985 --extra-output-bridge-port 19985
```
Only ever touches `satellites.yaml` - refuses a NORAD or port collision
with another satellite, and replaces (rather than duplicates) an
`extra_outputs` entry when you re-add one with the same name. If a
change here needs the `.grc` updated to match (a new frequency, say),
that's a separate manual step in GRC; the script says so when it applies.
See [Adding a satellite](adding-satellites.md) for the full `extra_outputs` picture.

**`ci_check.py`** - the portable subset of `preflight.py`'s checks that
can run with no GNU Radio, no Hamlib, and no real TLE file - what runs in
CI on every push. Safe to run locally too, any time:
```
python3 ci_check.py
```

**`regen_all.sh`** / **`Makefile`** - `./regen_all.sh` (or `make build`)
regenerates every `flowgraphs/*.grc` into its `.py` via `grcc`, then runs
`preflight.py` - one command instead of remembering to `grcc` each file
individually. Other targets: `make check`, `make check-live`,
`make status`, `make doctor`. In multi-station mode (see
[scripts-reference.md](scripts-reference.md), `station.py`) these act on one
station: `./regen_all.sh --radio mini`, or `GROUNDTRACK_STATION=mini make check`.

