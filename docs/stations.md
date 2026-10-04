# groundtrack: Stations (more than one radio)

[← back to README](../README.md)

For a ground station with more than one SDR/antenna system running
independently - say an Airspy R2 on a steerable beam, and an Airspy Mini
on a fixed helix, often both tracking at the same time. This page is the
whole story; with a single radio you can ignore it completely, and nothing
in the project behaves differently until you turn it on.

## The idea

Each radio/antenna system is a **station**: a complete folder of its own,
holding everything that belongs to that radio and nothing it shares with
another. The scripts themselves live once, in the project root, and are
never duplicated.

```
groundtrack/                  the scripts - one copy, shared
    run_passes.py  plan_passes.py  preflight.py  ...
    radios.yaml               which stations exist (you create it - see below)
    tle/amateur.txt           the ONE thing stations share
    beam/                       station "beam": the beam
        satellites.yaml       ... its own satellites, rig_port, rotor
        schedule.yaml
        pass_log.jsonl
        run_passes.lock       appears while beam's run_passes.py is running
        flowgraphs/           ... its own .grc files, with that SDR's device string
    helix/                     station "helix": the helix
        satellites.yaml       ... its own satellites and rig_port, NO rotor keys
        flowgraphs/
```

Every script acts on **one station at a time**. It picks the station,
changes into that folder, and from then on does exactly what it always has:
every relative path it already used (`satellites.yaml`, `flowgraphs/`,
`schedule.yaml`) simply resolves inside the station. That is why two
stations can't tangle - they don't touch the same files, so two copies of
`run_passes.py`, one per station, run side by side without knowing about
each other.

What makes the radios different lives in each station's own files, not in
some central table: `rig_port` and the rotor (`rot_host`/`rot_port`) in its
`satellites.yaml`, and the SDR device string inside each of its `.grc`
files. **A station with no `rot_host`/`rot_port` never has its antenna
moved** - the same mechanism that already lets you run with no rotor at all.

## When two receivers share one antenna

A station is one scheduler plus one rotor and Doppler setup - not one SDR. So if a
second receiver shares the beam (say an RTL-SDR on 2 m behind a diplexer, beside the
Airspy R2 on 70 cm), keep both in the **same** station. The beam can only point one
place, and one scheduler is what decides where. Two stations would each plan passes
with nothing to stop the second recording while the beam points somewhere else - and
preflight refuses two stations steering one rotor in any case.

By default that costs one flowgraph at a time: a 2 m pass and a 70 cm pass that overlap
compete (`plan_passes.py` flags it) - unless you pair them, which is for the rare case of
two satellites close together, below. One satellite can't be recorded on both bands at
once from one station (a station keys its satellites on NORAD, so the two entries would
collide); if you need that, give the second receiver a station of its own with no rotor
configured, and the first station moves the beam for both.

Each SDR needs its own record-only template, since its device string - and usually its
sample rate - differs. Name them `_record_only_template.grc` (the default) and
`_record_only_template_<anything>.grc` (say `_record_only_template_2m.grc`), all in the
station's `flowgraphs/`. Given a satellite's frequency, `new_record_only_satellite.py`
and the GUI's Add dialog pick the template whose own frequency is nearest - 145 MHz
can't be mistaken for 437 MHz - and say which. `--template` (or choosing in the dialog)
overrides it, and a frequency exactly between two templates is refused rather than
guessed. `new_record_only_satellite.py --radio BEAM --list-templates` shows each one's
frequency, band and SDR (and warns about two on the same band); add `--freq HZ` to see which
one that frequency would pick. Doppler needs nothing declared: each satellite entry carries its own downlink
frequency, and each flowgraph polls the port in its own `rig_freq_poller` block - the
station's `rig_port` for the Airspy's, a different one (4531, say) for the RTL-SDR's, which
`run_passes.py` reads from the `.grc` and gives a `rigctld` of its own.

## Recording two satellites at once

Sometimes two satellites are close together in the sky and you want both - one on 70 cm,
one on 2 m. You can, if their flowgraphs run on different receivers (the Airspy with Doppler
on `rig_port` 4532, the RTL-SDR whose flowgraph polls 4531) and you tell the scheduler to
**pair** the two passes. The beam only points one way, so you choose whose TLE steers it:
the *leader*. The other, the *companion*, rides along on its own receiver with its own,
independent Doppler, computed from its own TLE and frequency.

Nothing is declared in `satellites.yaml`: which receiver a satellite uses is read from the
Doppler port in its flowgraph's `rig_freq_poller` block, and `run_passes.py` starts a
`rigctld` on each such port. (An explicit `rig_port:` in a satellite's entry overrides what
the `.grc` says.) Preflight lists the receivers and checks that each port is clear of the
rotor, the relay ports and every other station.

You decide when the planner finds the overlap - `plan_passes.py --interactive` offers
"together, X steers the beam" for passes on different receivers - or afterwards with
`pair_passes.py` (also a button in the GUI):
```
python3 pair_passes.py                           # list the candidates, then choose
python3 pair_passes.py --pair 2 --leader HADES-L
python3 pair_passes.py --unpair 2
```
A pairing is written into `schedule.yaml` as `rides_with: "<norad>@<aos>"` on the companion
pass, shown by `show_queue.py` in a TOGETHER column, and checked by preflight (a pairing that
can't hold is reported, and `run_passes.py` ignores it). `run_passes.py` reads the schedule
when it starts, so restart it after changing pairings.

While both run, each pass's Doppler goes to its own receiver and the rotor follows the
leader. If the leader ends first the companion takes over the beam; if the companion's AOS
comes first, it steers until the leader arrives. Overlapping passes that are *not* paired
are unchanged: the first to start records and the rest wait.

## Turning it on

Stop `run_passes.py` and `relay.py` first. Then, from the project folder:

```
python3 migrate_to_stations.py --second-rig-port 4534 --dry-run     # look first
python3 migrate_to_stations.py --second-rig-port 4534 \
    --first-label "Beam (Az/El)" --second-label "Helix (fixed)" \
    --second-template ~/my_working_mini_flowgraph.grc
```

It moves your whole current fleet into `beam/` (`flowgraphs/` with `git mv`, so
history follows the files; `satellites.yaml`, `schedule.yaml`, `pass_log.jsonl`
alongside), creates an empty `helix/` beside it, and writes `radios.yaml`. The
only edit it makes inside a moved file is prefixing `../` to a relative
`tle_file`/`custom_tle_file`, since the shared `tle/` folder is now one level
up - a plain line edit, so your comments and formatting survive. Nothing else
needs changing: `script:` paths, ports and decoder paths all still resolve,
because a station folder is laid out exactly like the old project root.

It is built to be safe to try. It checks everything first and reports every
problem at once (a run in progress, compiled files stranded in the project
root - `doctor.py --fix` first - a folder that already exists, two stations
sharing a `rig_port`); `--dry-run` changes nothing; your `satellites.yaml`, `schedule.yaml` and
`pass_log.jsonl` - the files git doesn't track - are copied to
`.pre_stations_backup/` first (`flowgraphs/` is moved whole, so compiled and
untracked files in it travel too); and if any step fails, the steps already
done are undone.

**The second station's template is yours to supply.** `--second-template`
installs a flowgraph you already run on that radio as its
`_record_only_template.grc` (it's checked first: the tool must be able to
generate from it). It is deliberately *not* generated from R2's: the radios
differ in more than a device string - the sample rates they offer, and so the
decimation and filter settings that follow - so a copy of the other radio's
flowgraph with one line swapped would look right and not work. (As far as I
know the Airspy Mini offers 3 and 6 MS/s where the R2 offers 2.5 and 10.)
Without the option, `helix/flowgraphs/` is left empty.

**Adding the template after the migration** (the migration only runs once): copy
a flowgraph you already run on that radio to
`helix/flowgraphs/_record_only_template.grc`, then check the tool can use it,
without creating anything:

```
python3 new_record_only_satellite.py --radio helix --name TEST --norad 99999 --freq 437000000 --dry-run
```

It shows the flowgraph it would generate, or says exactly what's missing (no
Advanced File Sink, say). Until a template exists, adding a template-based
satellite on that station is refused with a message saying so - it never falls
back to another station's flowgraph.

Afterwards:

```
python3 preflight.py --radio beam
python3 preflight.py --radio helix      # a warning that it has no satellites yet is expected
git add helix                           # git mv already staged the renames
git status                             # review: renames staged; your own uncommitted edits still unstaged
git commit -m "move to multi-station layout"
```

`git add helix`, not `git add -A beam helix`: the latter would also commit any
uncommitted edits and untracked flowgraphs sitting in `beam/flowgraphs`. Those are
yours to commit when you choose - the migration only moves them.

`radios.yaml` itself is gitignored (it's your site's station list); copy
`radios.example.yaml` to see every field. `default:` names the station the GUI
opens on.

## Using it: the GUI

The window opens on `radios.yaml`'s default station (the beam, in the examples here) every time,
without asking, and never remembers the last session's choice - being surprised
about which radio you're pointed at is the wrong kind of surprise. `python3
groundtrack_gui.py --radio helix` opens on a specific one.

The **Active station** bar at the top is a switch: click a station to point the
whole window at it. The active one is green and sunken, and the bar says what
that choice means physically:

- an orange **BEAM CONTROL ON (host:port)** badge if the station has a rotor,
  or a muted **no rotor - antenna never moved** if it doesn't - so you never
  start tracking on the beam without having seen it say so;
- the station's label and `rig_port`;
- a **●** on any station whose `run_passes.py` is running, and
  `run_passes.py RUNNING (PID ...)` in the info line. These are re-read every
  few seconds, since runs start and stop in terminals the window doesn't control.

Switching changes the working folder, the table, the window title (`[helix]`),
and clears the output pane - leftover output from the other radio, sitting
there after a switch, is exactly what gets misread as current. If the station
you're switching to already has a `run_passes.py` running, you get an
information dialog saying so: switching only changes what the window shows and
controls; that run carries on, untouched, in its own terminal. **Start
run_passes.py** on a station that's already running is refused up front
(`run_passes.py` would refuse anyway, via the same lock file - this just says so
now, instead of in a terminal window that opens, prints an error and sits
there). Every button - Add satellite, Edit, the planner, Start run_passes -
acts on the active station.

## Using it: the command line

Every script takes `--radio NAME`, or reads `GROUNDTRACK_STATION`, or - at a
real terminal only - asks:

```
python3 run_passes.py --radio helix --verbose
GROUNDTRACK_STATION=helix python3 plan_passes.py
python3 preflight.py            # asks: Which station?
./regen_all.sh --radio helix     # also: GROUNDTRACK_STATION=helix make check
```

`--radio` beats the environment variable, which beats being asked. There is
deliberately never a silent default: start the wrong radio's tracking and you
may be moving an antenna, which is worth one extra keystroke. With no terminal
and no choice, a script refuses and says how to choose. `--help` works without
choosing. Names are case-insensitive.

**Running both at once** is just two copies of `run_passes.py`, one per station
- two terminals, or the GUI's **Start run_passes.py** on one station, switch,
then again on the other. Each has its own lock, its own schedule, its own
`rigctld` on its own port. (`rotctld` stays yours to start, for the beam only.)

## Adding satellites

The GUI's **Add satellite...** and the command-line tools all act on the
station you're in. Each station keeps its own `_record_only_template.grc`
carrying that radio's device string, so `new_record_only_satellite.py --radio
helix` builds a helix flowgraph and `--radio beam` a beam one, from the same tool.
Automatic port assignment ("next free port") also looks at the *other*
stations' ports, so a new satellite on the helix isn't handed 9101 when the beam's
GEOSCAN-1 already holds it.

## The shared TLE file

The one thing stations share. Point every station's `tle_file` (and
`custom_tle_file`, if you use one) at the same path - `../tle/amateur.txt`. A
satellite tracked on both radios is fetched once.

`update_tle.py` is the one script that deliberately spans stations, and ignores
`--radio`: it rebuilds the whole TLE file from whichever satellites it's given,
so reading only one station's list would silently drop the other's TLEs the
next time it ran. It refuses, rather than guess, if two stations name different
TLE files.

## Safety checks

- `preflight.py`, inside a station, also checks **nothing is shared with
  another station**: `rig_port` (two `run_passes.py` would both start `rigctld`
  on it), the rotor (two stations steering one antenna), and every relay,
  bridge and flowgraph port. It reads the other station's file for you, since
  that's a property of two files nothing else would notice.
- `doctor.py`'s port and process checks read every station's configuration - each
  station's `rig_port` and local rotor port, and the relay/bridge ports and
  compiled scripts of every enabled satellite - and a busy `rig_port` is reported
  as expected while that station's own `run_passes.py` is running.
- It also **warns** when enabled satellites in two stations share a NORAD number
  under different names (and says if the stations take its TLE from different
  places). The same number and name on both radios is normal and stays quiet.
- `ci_check.py` fails any script that reads station files without choosing a
  station, and any that launches another script through `sys.executable` by a
  bare filename - written literally, or via a variable assigned one (a bare name
  isn't found once the script has changed folder) - so a future script can't
  quietly forget. It reads the code rather than matching text, so comments can't
  fool it; what it can't see is a filename built up dynamically at run time.

## Troubleshooting

**`station: no station selected, and there's no terminal to ask on`** - the
script was run somewhere it can't prompt (cron, a pipe, a captured subprocess).
Pass `--radio NAME` or set `GROUNDTRACK_STATION`.

**`station: unknown station 'x' (from GROUNDTRACK_STATION)`** - the variable is
set in your shell from earlier. `unset GROUNDTRACK_STATION`. (The GUI honours it
too, as an explicit choice - `--radio` beats it.)

**The GUI opened on the wrong station** - it opens on `default:` in
`radios.yaml`, unless `--radio` or `GROUNDTRACK_STATION` says otherwise. The
title bar and badge always show where you are.

**`preflight.py --radio helix` warns "none yet"** - expected for a station with no
satellites; add one.

**`stations disagree about tle_file`** - point every station at the same file.

## Known limitations

- Nothing separates the two radios' *recordings*: each flowgraph's Advanced File
  Sink writes wherever its own Base Directory says. If both stations record the
  same satellite at once into one folder, check the filenames can't collide.
- `ground_station` (lat/lon/alt) is repeated in each station's `satellites.yaml`;
  it never changes, but if it ever does, change both.

## Renaming a station

```
python3 rename_station.py r2 BEAM
python3 rename_station.py mini HELIX --label "Helix (fixed, no rotor)"
python3 rename_station.py r2 BEAM --dry-run
```

This changes the name in `radios.yaml` (what the GUI's buttons and `--radio` use), the
`default:` if it named that station, and the folder - to the new name lowercased unless
you pass `--dir` - with `git mv` when the folder holds tracked files, so history follows
and the renames are staged for you to commit. It checks first and reports every problem
together (a station that's running, a name already taken, a folder that exists), keeps
your comments in `radios.yaml`, and undoes itself if any step fails. Afterwards, change
any `GROUNDTRACK_STATION` export in `~/.bashrc` or a launcher - a stale one makes the GUI
refuse with "unknown station" - and restart the GUI.

## Going back

Stop everything, then reverse the move: `git mv beam/flowgraphs flowgraphs`, move
`beam/satellites.yaml`, `schedule.yaml` and `pass_log.jsonl` back to the project
root, remove the `../` from `tle_file`/`custom_tle_file` (or copy back the
original from `.pre_stations_backup/satellites.yaml`), and delete `radios.yaml`.
The scripts behave as before the moment `radios.yaml` is gone.
