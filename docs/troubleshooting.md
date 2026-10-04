# groundtrack: Troubleshooting

[← back to README](../README.md)

## Troubleshooting

Symptoms we've actually hit, in the order worth checking:

**`station: no station selected, and there's no terminal to ask on` (or
`unknown station`, or `stations disagree about tle_file`)**
Multi-station mode (a `radios.yaml` exists) - see [Stations](stations.md),
"Troubleshooting". Pass `--radio NAME` or set `GROUNDTRACK_STATION`.

**`preflight.py` fails with `embedded-block modules sit next to flowgraphs/<n>.py`, or a
flowgraph dies at launch with `ModuleNotFoundError`**
A compiled flowgraph imports a small module GRC generates for each embedded Python
block (`rig_freq_poller`, say - named `<id>_<block>.py`), and it has to sit next to
the flowgraph's `.py`. It goes missing if the flowgraph was compiled by a bare
`grcc` from another folder, or the module was deleted. Fix: `./regen_all.sh` (add
`--radio NAME` with several radios), or `grcc -o flowgraphs flowgraphs/<n>.grc`.
`doctor.py --fix` moves one that landed in the wrong folder into place and never
deletes it.

**`[warn]` about a TLE coming from `custom_tle_file` (in `preflight.py`,
`plan_passes.py` or `run_passes.py`)**
Your custom file always wins for any NORAD it lists, whatever the catalog says, and
every use prints how old the entry is. The warning means it's more than 14 days old
or the catalog now has newer data. Not a failure: refresh the entry from a source you
trust, or delete it to follow the catalog again. See
[scripts-reference.md](scripts-reference.md).

**`[warn] no NORAD names different satellites in different stations`**
Enabled satellites in two stations share a NORAD under different names. The same
number and name on both radios is normal and stays quiet. If they really are
different satellites, one number is wrong: look it up (SatNOGS DB) and fix it with
`python3 edit_satellite.py --radio NAME "<satellite>" --norad N`. The warning also
says if the stations take that TLE from different places, meaning they're tracking
different orbits for the number.

**A satellite's pass "exits early" over and over, with `error: unrecognized
arguments: --record-iq 1` (or another argument error) printed each time**
That satellite is toggle-capable (its `.grc` is wired for it, or it has
`record_iq_toggle: true`), so `run_passes.py` passes it `--record-iq`, but its compiled flowgraph doesn't
have a `record_iq` Parameter block, so it rejects the flag and exits
immediately. Two ways out: wire the `.grc` (a `record_iq` Parameter block
feeding the Advanced File Sink's Record On Start) with `python3
wire_record_iq.py <name>` followed by `./regen_all.sh` - see [Adding a
satellite](adding-satellites.md), "Toggling IQ recording" - or, if you don't
need the toggle for that satellite, turn the flag back off:
`python3 edit_satellite.py <name> --no-record-iq-toggle`. (Either way the
satellite keeps recording IQ exactly as its `.grc` always did; the flag only
controls whether `run_passes.py` passes an option to switch it per run.) `python3
preflight.py` flags this mismatch before a run ever starts, which is the
reason to run it (or `doctor.py`) first. Two backstops exist for when that
step is skipped: `run_passes.py` asks each relevant compiled script whether
it accepts `--record-iq` at startup and, if it doesn't, prints a warning and
launches that satellite without the flag instead of crashing it every pass;
and `edit_satellite.py --record-iq-toggle` now checks the `.grc` itself and
refuses to set the flag on an unwired satellite, so the bad state is hard to
create in the first place. Separately, a flowgraph that does die at launch is
retried at most 3 times, 15 seconds apart, then given up on for that pass -
earlier versions relaunched it every few seconds for the entire pass window
(one overnight run logged hundreds of crash/relaunch cycles from a single
bad pass). Each failed attempt is recorded in `pass_log.jsonl`;
`python3 show_pass_log.py --failures-only` shows what happened.

**`Connection refused` from `test_downstream.py` or when connecting your
decoder to a consumer port**
`relay.py` isn't running (or died). Check with `ps aux | grep relay.py`,
or just run `python3 doctor.py` - it lists exactly what's holding each
fleet port, and if a port shows `free` when it should show `relay.py`,
that's your answer. Start it: `nohup python3 relay.py > relay.log 2>&1 &`

**Real passes happen with signal/frames visible in the console, but
nothing shows up in your downstream decoder**
Almost certainly: only one satellite's consumer port has a decoder
connected to it, and a different satellite's pass just happened. The
relay serves every relay-using satellite's consumer port simultaneously
and continuously - it never switches which port is "active." Your
decoder needs **one persistent connection per relay-using satellite, all
open at once** - not one connection you re-point before each pass. See [The relay](relay-and-bridging.md) for the exact setup. Verify with `tail -f relay.log`:
you should see one `consumer connected` line per relay-using satellite
that persists,
not one connection that comes and goes.

**Frames arrive but your decoder can't parse them / shows garbage**
First check you're not just looking at *synthetic* test frames
(`test_downstream.py` sends these automatically when no real capture
exists yet - they're garbage payload by design, not a bug). If you
expected real captured data, double check your decoder's **Port** field
matches the satellite you think you're testing - `satellites.yaml` maps
each satellite to a specific `consumer_port` (8101=GEOSCAN-1,
8102=GEOSCAN-2, etc.), and pointing a "GEOSCAN-2" decoder tab at port 8101
will silently show you GEOSCAN-1's traffic instead, with no error at all.
This exact mixup happened once already in this setup - the tab was
labeled correctly but the port field wasn't updated to match.

**A pass looks completely normal in the console and in the `.kss` file,
but SatsDecoder shows one giant garbage frame (e.g. misparsed as
`INNOSAT16`, hundreds of bytes long) and then nothing further for the
rest of the pass**
This is the missing-KISS-framing bug, not a relay or SatsDecoder
problem - see "Why every `.grc` needs a `kiss_encode_pdu` block" above
for the full explanation. Confirm by checking whether that satellite's
`.grc` has `kiss_encode_pdu` between the decoder and `network_socket_pdu`;
if it's wired directly, that's the cause. Fix: add the block, `grcc` (or
`./regen_all.sh` for everything), and re-verify with the KISS File
Source -> `kiss_encode_pdu` -> Socket PDU test rig described in that
section before trusting the next live pass.

**`SatsDecoder` shows "Connection lost" mid-pass, decoder tab drops with
signal still visibly active**
This was a real bug in `SatsDecoder` itself, not this toolkit -
`kiss_read_stream()` returned the same falsy value (`b''`) for both "no
data yet" and "connection actually closed," so a perfectly ordinary empty
KISS frame could get misread as a dropped connection partway through a
pass. Fixed upstream in commit `d94ff8e` ("KISS reader refact"), first
available in the `nightly` tag - update with:
```
cd ~/SatsDecoder
git fetch --tags
git checkout nightly
```
(if you have a local `nightly` tag already cached from before this fix
landed, `git fetch --tags` won't move it - `git tag -d nightly` first,
then re-fetch, or you'll silently stay on the old code.)
`relay.py`'s TCP keepalive is a separate, complementary safeguard against
a genuinely stalled connection - it does not fix this specific bug, so
update `SatsDecoder` rather than relying on keepalive alone.

**Terminal looks "stuck" after backgrounding `relay.py`**
It isn't - `python3 relay.py &` sometimes prints its startup lines a
moment *after* bash already returned your prompt, which looks like a
hang but isn't one (press Enter and the prompt reappears clean). Avoid
the confusion entirely by redirecting its output instead of leaving it
attached to your terminal: `nohup python3 relay.py > relay.log 2>&1 &`,
then `tail -f relay.log` whenever you want to check on it.

**A port that should be free shows `IN USE` and you don't know why**
Run `python3 doctor.py` - it identifies the exact PID and command holding
every fleet port. This has caught real orphaned processes before (e.g. a
`rigctld` left running from a folder that had since been moved to Trash,
still holding port 4532 indefinitely). Kill the specific PID it reports,
or `pkill -f <name>` for a broader cleanup.

**Config edits don't seem to take effect**
`.grc` files are source; `run_passes.py` and `preflight.py` execute the
compiled `.py` next to them. Any edit to a `.grc` needs
`grcc flowgraphs/geoscanN.grc` before it does anything. `preflight.py`
flags this automatically ("`.py` is up to date with `.grc`").

**`network_socket_pdu` reverts to `TCP_SERVER` after editing/regenerating
a `.grc` in GNU Radio Companion**
This has happened more than once - something about re-adding or resolving
that block in GRC resets it to its default. There's no permanent fix on
our side for GRC's behavior, so make checking a habit:
`python3 preflight.py` explicitly checks this on every satellite and will
fail loudly if it's reverted, rather than you discovering it mid-pass.

**Multiple copies of the `fleet` folder causing confusion (Trash, Downloads,
Desktop all having their own)**
`python3 doctor.py`'s second section lists every folder under your home
directory containing both `satellites.yaml` and `run_passes.py`, with each
one's last-modified time, so you can see at a glance which is real and
which are stale duplicates worth deleting.

**`TypeError` or YAML parse errors when running `run_passes.py` or
`plan_passes.py`**
Almost always a stray value in `satellites.yaml` - a quoted number
(`freq_hz: "436160000"`), a typo (`freq_hz: 436160=3000`), or similar.
`preflight.py` validates every field's type explicitly and will name the
exact satellite and field at fault rather than you hunting through a
traceback.

**Not sure if something is a real problem at all**
Start with `python3 doctor.py` every time - it's a strict superset of
`preflight.py` and was specifically built to catch the "environment"
class of problems (wrong folder, stale process, occupied port) that
config-only checks can't see.

**A satellite's `.py` seems to go missing, or `preflight.py` briefly shows
"doesn't exist" for a `flowgraphs/*.py` you just compiled**
A manual `grcc some.grc` (outside any tool here) writes its output to
whatever directory you ran it from, not the `.grc`'s own folder - see
"Known caveats" below. You shouldn't need to chase this down by hand:
plain `python3 preflight.py` (no `--fix`, no `doctor.py` needed) finds and
relocates a stray compile automatically, before its own other checks even
run. If a satellite still looks wrong after that, the stray wasn't the
cause - the fix would have already resolved it.

## Known caveats

- **GRC doesn't reload a `.grc` that changed on disk**: `wire_record_iq.py`,
  `vet_grc.py --fix`, and `locate_decoders.py` (and any edit made from
  outside GRC) change the file, but a GRC window that already has it open
  keeps showing the copy it loaded - the new block or renamed field simply
  isn't there on screen. That's a stale view, not a failed edit (check with
  `grep`, or `python3 preflight.py`, which reads the real file), and it's a
  dangerous one to act on: saving, or Generate/Run - which rebuilds the
  `.py` from what's on screen - silently writes the old copy back over the
  change. Close the flowgraph in GRC (discarding, not saving) before
  running one of these tools, and reopen it afterwards.
- **One flowgraph at a time per receiver**: overlapping passes behave as ever - the
  first to start records, the rest wait, and a running satellite is not pre-empted.
  The exception is two passes on *different* receivers that you've paired
  (`pair_passes.py`): they run together, and the leader's TLE steers the beam. See
  [Recording two satellites at once](stations.md#recording-two-satellites-at-once).
- **rigctld's Dummy backend response format**: the poller inside each
  `.grc` expects `f\n` to return a bare number - confirmed working, but
  worth re-checking if you ever change Hamlib versions.
- **Doppler sign/magnitude**: sanity-check `doppler_hz()` in
  `run_passes.py` against a known pass before trusting a real recording.
- **Rotor update behavior**: `find_lead_ahead_target()`/`maybe_update_rotor()`
  (ported from [N6RFM/Gpredict_K4KDR_N6RFM](https://github.com/N6RFM/Gpredict_K4KDR_N6RFM))
  only send a new position once the satellite has drifted `rot_threshold_deg`
  (set in `satellites.yaml`'s top level, default `5.0` if unset) from
  wherever the rotor was last commanded, and when they do, they send a
  predicted future point, not the satellite's instantaneous position -
  see [Tracking Control](tracking-control.md) for the full explanation. There is
  no time-based forced send at all anymore; a long quiet stretch during a
  slow-moving part of a pass is correct, not a sign anything's stuck.
  Comparisons use the rotor's own live, polled position
  (`Rotctld.get_pos()`), not the last-commanded value - measure your
  rotor's actual capability with `measure_rotor_speed.py` before assuming
  a tighter threshold is safe; that same tool also reveals a real,
  separate thing worth knowing: many rotors have an inherent position
  precision limit (this project's own test rotor sits around 1 degree)
  that a too-tight tolerance will mistake for the rotor being stuck.
  `test_rotor_leadahead.py` exercises the tracking logic directly against
  your real rotctld with synthetic, predictable motion if you want to
  verify it independently.
- Hand-authored `.grc` blocks (`epy_block`s, `network_socket_pdu`) were
  written outside GNU Radio Companion - open each block's properties
  dialog once after import to let GRC regenerate anything it flags.
- **`SatsDecoder` version**: use the `nightly` tag or later (commit
  `d94ff8e`+). Anything at or before release `0.3.6` has the mid-pass
  false-disconnect bug described in Troubleshooting.
- **`kiss_encode_pdu` is required on every decode-and-relay satellite's
  `.grc`**: `satellites_satellite_decoder`'s PDU output is unframed;
  `network_socket_pdu` has no KISS awareness of its own. Without
  `kiss_encode_pdu` between them, frames reach SatsDecoder unframed and
  get silently dropped or misparsed. See "Why every `.grc` needs a
  `kiss_encode_pdu` block" above. Check this first on any newly-added
  decode-and-relay satellite's `.grc`, since it's easy to copy the
  decoder wiring but forget this one extra hop. Doesn't apply to
  recording-only satellites - they have no `network_socket_pdu` at all.

