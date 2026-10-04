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
    r2/                       station "r2": the beam
        satellites.yaml       ... its own satellites, rig_port, rotor
        schedule.yaml
        pass_log.jsonl
        run_passes.lock       appears while r2's run_passes.py is running
        flowgraphs/           ... its own .grc files, with R2's device string
    mini/                     station "mini": the helix
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

## Turning it on

Stop `run_passes.py` and `relay.py` first. Then, from the project folder:

```
python3 migrate_to_stations.py --second-rig-port 4534 --dry-run     # look first
python3 migrate_to_stations.py --second-rig-port 4534 \
    --first-label "R2 + beam (Az/El)" --second-label "Mini + helix (fixed)" \
    --second-template ~/my_working_mini_flowgraph.grc
```

It moves your whole current fleet into `r2/` (`flowgraphs/` with `git mv`, so
history follows the files; `satellites.yaml`, `schedule.yaml`, `pass_log.jsonl`
alongside), creates an empty `mini/` beside it, and writes `radios.yaml`. The
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
Without the option, `mini/flowgraphs/` is left empty.

**Adding the template after the migration** (the migration only runs once): copy
a flowgraph you already run on that radio to
`mini/flowgraphs/_record_only_template.grc`, then check the tool can use it,
without creating anything:

```
python3 new_record_only_satellite.py --radio mini --name TEST --norad 99999 --freq 437000000 --dry-run
```

It shows the flowgraph it would generate, or says exactly what's missing (no
Advanced File Sink, say). Until a template exists, adding a template-based
satellite on that station is refused with a message saying so - it never falls
back to another station's flowgraph.

Afterwards:

```
python3 preflight.py --radio r2
python3 preflight.py --radio mini      # a warning that it has no satellites yet is expected
git add mini                           # git mv already staged the renames
git status                             # review: renames staged; your own uncommitted edits still unstaged
git commit -m "move to multi-station layout"
```

`git add mini`, not `git add -A r2 mini`: the latter would also commit any
uncommitted edits and untracked flowgraphs sitting in `r2/flowgraphs`. Those are
yours to commit when you choose - the migration only moves them.

`radios.yaml` itself is gitignored (it's your site's station list); copy
`radios.example.yaml` to see every field. `default:` names the station the GUI
opens on.

## Using it: the GUI

The window opens on `radios.yaml`'s default station (R2, the beam) every time,
without asking, and never remembers the last session's choice - being surprised
about which radio you're pointed at is the wrong kind of surprise. `python3
groundtrack_gui.py --radio mini` opens on a specific one.

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

Switching changes the working folder, the table, the window title (`[mini]`),
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
python3 run_passes.py --radio mini --verbose
GROUNDTRACK_STATION=mini python3 plan_passes.py
python3 preflight.py            # asks: Which station?
./regen_all.sh --radio mini     # also: GROUNDTRACK_STATION=mini make check
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
mini` builds a mini flowgraph and `--radio r2` an R2 one, from the same tool.
Automatic port assignment ("next free port") also looks at the *other*
stations' ports, so a new satellite on the mini isn't handed 9101 when R2's
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

**`preflight.py --radio mini` warns "none yet"** - expected for a station with no
satellites; add one.

**`stations disagree about tle_file`** - point every station at the same file.

## Known limitations

- `doctor.py`'s port list is hardcoded and predates stations, so it doesn't
  check the second station's `rig_port`; `preflight.py` does.
- Nothing separates the two radios' *recordings*: each flowgraph's Advanced File
  Sink writes wherever its own Base Directory says. If both stations record the
  same satellite at once into one folder, check the filenames can't collide.
- `ground_station` (lat/lon/alt) is repeated in each station's `satellites.yaml`;
  it never changes, but if it ever does, change both.

## Going back

Stop everything, then reverse the move: `git mv r2/flowgraphs flowgraphs`, move
`r2/satellites.yaml`, `schedule.yaml` and `pass_log.jsonl` back to the project
root, remove the `../` from `tle_file`/`custom_tle_file` (or copy back the
original from `.pre_stations_backup/satellites.yaml`), and delete `radios.yaml`.
The scripts behave as before the moment `radios.yaml` is gone.
