# groundtrack GUI

`groundtrack_gui.py` is a Tkinter GUI over the existing command-line
tools - a way to see the whole fleet at a glance and manage satellites
with a click, without needing to remember every script's exact flags.

**It's a layer on top of the CLI tools, not a replacement for them.** If
it's ever abandoned, deleting `groundtrack_gui.py` undoes the entire
thing - nothing else in the repo depends on it, and nothing about how
the CLI tools work changes whether or not this GUI exists alongside
them.

## The one rule everything else follows

**The GUI never edits `satellites.yaml` (or any other config) directly,
and never reimplements a script's logic.** Every action is a real
subprocess call to the exact same script and flags you'd type at the
terminal yourself - one code path for "how do I add a satellite," not
two that can quietly drift apart from each other over time. The only
things the GUI does entirely on its own are read-only: parsing
`satellites.yaml` to build the table, and checking file existence/
modification times for the "Gaps found" column.

## Stations (only when `radios.yaml` exists)

With more than one radio/antenna system (see [Stations](docs/stations.md)), a
row across the top - **Active station** - switches the whole window between
them. It opens on `radios.yaml`'s default (R2, the beam) every time, never on
whatever you used last; `--radio NAME` opens it on another. The active
station is green and sunken, with a badge you can't miss: orange **BEAM
CONTROL ON (host:port)** if that station has a rotor, muted **no rotor -
antenna never moved** if not. A **●** marks any station whose `run_passes.py`
is running (re-read every few seconds - those runs live in terminals this
window doesn't control).

Switching changes the working folder, the table, the title (`[mini]`), and
clears the output pane. Switching to a station that's already running
`run_passes.py` tells you so - it only changes what this window shows; that
run carries on untouched - and **Start run_passes.py** on such a station is
refused up front rather than in a terminal that opens and immediately fails.
Every button below acts on the active station. With no `radios.yaml` none of
this appears and nothing changes.

## Satellite management

Left to right, the buttons run roughly in workflow order - view, create,
modify, toggle state, build, then the one destructive action set apart
by a divider:

| Button | What it actually runs |
|---|---|
| Refresh | (read-only - re-parses `satellites.yaml` and re-checks each satellite's files) |
| Add satellite... | `add_satellite.py --name ... --norad ... --freq ...` (plus `--record-only` if checked, plus `--producer-port`/`--consumer-port` if you've overridden the suggested defaults) - or, if Recording-only and "Also build its .grc from a template" are both checked, `new_record_only_satellite.py --name ... --norad ... --freq ... --template ... --yes` instead (plus `--record-iq-toggle` if checked), which builds the .grc, adds the entry, and compiles it in one step |
| Edit selected | `edit_satellite.py NAME` with whichever fields you changed - see below |
| Enable / Disable selected | `toggle_satellite.py --enable/--disable NAME` |
| Regenerate .grc for selected | `grcc flowgraphs/<name>.grc` |
| Vet selected .grc | `vet_grc.py flowgraphs/<name>.grc` - read-only, shown in the output pane |
| Vet --fix selected .grc | `vet_grc.py --fix flowgraphs/<name>.grc` - confirms first (this one writes to the `.grc`), then shows the diff in the output pane |
| Delete selected | `delete_satellite.py NAME --yes` |

Both **Add satellite...** and **Edit selected** open a real form, not a
chain of popups - every field visible at once, and critically, **a
failure leaves the form open with everything you typed still there**
rather than losing your input. `run_cmd()` returns `(returncode,
output)` specifically so these dialogs can tell success from failure and
only close themselves on success.

## Editing a satellite

**Edit selected** opens with the satellite's current values pre-filled:
NORAD, frequency, min elevation, producer/consumer ports (if it uses the
relay), and an Enabled checkbox. **Save only ever writes to
`satellites.yaml` - it never touches the `.grc`.** If a change here (a
new frequency, a corrected NORAD) needs the `.grc` updated to match,
that's a separate, deliberate step you do yourself in GRC - `groundtrack`
doesn't automate `.grc` editing anywhere, after repeatedly finding that
kind of automation more fragile than doing it by hand (a stray internal
`id` field, blocks left unconfigured instead of removed, an orphaned
embedded-Python block left disconnected on the canvas - three separate
real bugs, across three separate satellites).

### extra_outputs

Some satellites have a second (or third) live output that a specific
downstream app connects to directly, bypassing `relay.py` entirely - see
[docs/adding-satellites.md](docs/adding-satellites.md) for the full rationale
(`relay.py` is a plain TCP byte-forwarder; a protocol like ZeroMQ
PUB/SUB already handles the connect/disconnect robustness it exists to
provide for raw TCP, so there's no reason to route it through the
relay).

The Edit dialog shows a live list of the satellite's current
`extra_outputs`, with **Add output...** and **Remove selected** buttons.
Each one is its own `edit_satellite.py` call and takes effect
immediately - separately from the **Save** button, which only covers the
fields above it (NORAD, frequency, ports, enabled). This is called out
directly in the dialog since it's a real, easy thing to miss otherwise.

**Add output...** opens a small sub-form: a name, a protocol dropdown
(`tcp_server` / `tcp_client` / `tcp_bridge` / `zeromq_pub`), the block's
exact name in the `.grc`, and a port-or-address field that relabels
itself and pre-fills a sensible default depending on which protocol is
selected. Choosing `tcp_bridge` also reveals a second field, **Bridge
port**, since that protocol needs both the flowgraph's own port and the
separate port the real downstream consumer should connect to - see [The tcp_bridge](docs/architecture.md) in the architecture doc for why a `TCP_SERVER` flowgraph needs this
extra step where the other three protocols don't.

At the top of that sub-form is a **"Copy from existing output"**
dropdown, listing every `extra_outputs` entry across every satellite
currently configured (e.g. `ASRTU-1_SSDV: ssdv_viewer (tcp_bridge, port
9985, bridge 19985)`). Picking one pre-fills protocol, block, and
port/address (and bridge port, for `tcp_bridge`) from that entry as a
starting point - the new output's own name is left blank, since that
should be specific to the satellite you're adding it to, not copied
verbatim. This exists because satellites that need `extra_outputs` tend
to come in families sharing the same downstream app and connection shape
(ASRTU-1_SSDV and BY70-4 both feed the same SSDV viewer and telemetry
upload agent, sharing the same `bridge_port` since only one of them is
ever actually transmitting at a time) - once one satellite's outputs are
set up correctly, the next one shouldn't need retyping protocol/block
conventions from scratch.

`edit_satellite.py --extra-output-name NAME ...` only ever touches
`satellites.yaml`. If the `.grc`'s actual block (its name, port, or
address) needs to change too, that's still a separate, manual edit to
the `.grc` itself - the script says so after any `extra_outputs` change.

## Why some buttons open a new terminal window instead

Three actions never exit on their own, or need real keyboard input the
GUI has no way to provide:

- **`relay.py`** - a persistent process, meant to be started once and
  left running for an entire session
- **`tcp_bridge.py`** - same reasoning, for satellites whose flowgraph
  runs its own `TCP_SERVER` instead of connecting out as a client
- **`run_passes.py`** - waits indefinitely for AOS and keeps running
  until you stop it
- **`plan_passes.py --interactive`** - prompts for a y/n answer per pass
  on stdin

Running any of these the same way as `preflight.py` (a blocking,
output-captured subprocess call) would freeze the entire GUI - Tkinter's
event loop can't do anything else while waiting on a process that never
finishes, and it has no way to type an answer into one that's waiting on
stdin. Instead, these four are launched as fully detached processes in
their own terminal window (`gnome-terminal`, `konsole`,
`xfce4-terminal`, `x-terminal-emulator`, or `xterm` - whichever is found
first), so you can watch live output and Ctrl-C them independently of
the GUI. If none of those terminal emulators are installed, the GUI
tells you the exact command to run by hand instead of failing silently.

Each of these is launched via a shared `spawn_in_terminal()` helper that
explicitly `cd`s to the repo root first and holds the window open after
the process exits, success or crash - without both of those, a terminal
emulator like `gnome-terminal` can silently start in the wrong directory
(its client/server model doesn't reliably inherit this process's cwd)
and close instantly the moment the command inside it exits, hiding any
real error before you can read it.

**The GUI has no idea what state these are in once launched.** It
doesn't know if `relay.py` is still running, crashed, or was closed -
that's a real limitation of the detached-process approach, traded
deliberately for never risking a frozen GUI. `doctor.py`'s own
process-detection (already built, already used from the CLI) would be
the natural way to surface "is this actually running" back in the GUI
later, if that becomes worth doing.

## Checks, TLE data, and the pass scheduler

| Button | What it actually runs |
|---|---|
| Run preflight.py (full check) | exactly that, captured and shown in the output pane |
| Run doctor.py | exactly that |
| Run doctor.py --fix | actually removes/moves stray compiled files it finds, rather than just printing the commands |
| Run update_tle.py | `update_tle.py` with no flags - fetches every configured satellite's TLE from SatNOGS in one bulk download (including "temporary ID" satellites too new for Celestrak's official catalog), falling back to an individual Celestrak lookup per-satellite for anything SatNOGS doesn't have |
| Show schedule | `show_queue.py` |
| Plan passes (auto-approve) | prompts for hours-ahead, then `plan_passes.py --hours N` |
| Plan passes (interactive, new window) | same prompt, then `plan_passes.py --hours N --interactive` in its own terminal (see above) |
| Toggle IQ for pass (new window) | `toggle_pass_record_iq.py` in its own terminal - needs real stdin for its interactive pass selection, same reasoning as everywhere else that prompts |
| Show pass log | `show_pass_log.py` |
| Start relay.py / Start tcp_bridge.py (new window) | `relay.py --verbose` / `tcp_bridge.py --verbose`, each in its own terminal |
| Start run_passes.py (new window) | prompts for the status line update interval (default 5s), then `run_passes.py --verbose --status-interval N`, plus `--no-preposition` and `--record-iq no` if their checkboxes are unchecked, in its own terminal |

Two checkboxes sit next to that button. **Pre-position rotor for next
pass** (checked by default) moves the rotor toward wherever the next
approved pass will rise as soon as the current one ends, instead of
leaving it wherever the pass happened to finish. On a station with no rotor
(no `rot_host`/`rot_port` in its config) it's greyed out and says so, and
`--no-preposition` is never passed for it; the tick you chose for the beam
station is still there when you switch back. **Record IQ** (checked
by default) only actually affects satellites whose `.grc` is wired for
the toggle (detected automatically) - see
[Adding a satellite](docs/adding-satellites.md)
for what that requires; every other satellite is unaffected regardless
of this checkbox's state.

Every captured (non-terminal) Python subprocess call runs with `-u`
(unbuffered), inserted automatically by `run_cmd()`. Without it, a
script's stdout switches from line-buffered to fully block-buffered the
moment it's a pipe rather than a real terminal - `doctor.py` calling
`preflight.py` as its own nested subprocess is exactly the case that
broke without this: the outer script's buffered output could sit
unflushed while the inner one ran, producing output that never
completed at all rather than just appearing out of order.

## The "Gaps found" column

A handful of cheap checks run automatically, done directly in Python
with no subprocess call at all, so they update instantly on every
refresh:

- does `flowgraphs/<name>.grc` exist?
- does `flowgraphs/<name>.py` exist? (has it ever been `grcc`'d?)
- is the `.py` *older* than the `.grc`? (stale - needs a fresh `grcc`)
- does the satellite have exactly one of `producer_port`/`consumer_port`
  rather than both or neither? (a malformed relay config)

These are deliberately narrow and not a replacement for `preflight.py`'s
full check (which also validates `extra_outputs`, decoder file paths,
TLE coverage, and frequency agreement between the `.grc` and
`satellites.yaml`) - they exist purely to make the table itself useful
without needing to click anything first. Any satellite showing gaps here
is worth a "Run preflight.py" click for the complete picture.

## The output pane

**Clear**, **Copy**, and **Save to file...** sit above the pane itself.
Copy and Save capture whatever's currently shown; Clear empties it.
There's no running log across multiple commands - each action replaces
what was there before.

## Known limitations

- No confirmation dialog before Enable/Disable fires - it runs the
  moment you click (Delete and removing an `extra_outputs` entry both
  do confirm first)
- No live *output* for anything launched in a new window (relay, execution,
  interactive planning) - see above. (With stations, the station bar does
  show whether a station's `run_passes.py` is running, via its lock file -
  but not what it's doing.)
- The table's slug-guessing (turning a satellite's `script:` field back
  into its likely `.grc`/`.py` names) is a rough approximation of
  `add_satellite.py`'s real slugify logic, close enough for display but
  not authoritative

## Running it

```
cd groundtrack
python3 groundtrack_gui.py
```

In the classic single-folder layout, run it from the repo root, since it
reads `satellites.yaml` there. With `radios.yaml` it works from anywhere:
it changes into the active station's folder itself, and launches every
other script by absolute path.
