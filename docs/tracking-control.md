# groundtrack: Tracking Control

[← back to Architecture](architecture.md)

## How Doppler control works

- `run_passes.py` starts **one** `rigctld -m 1 -t <rig_port>` (Hamlib's
  Dummy rig backend - just a frequency register, no real hardware behind
  it) when it launches, and keeps it running for the whole session,
  independent of which satellite is active.
- `run_passes.py` is the only thing that ever *sets* frequency on it
  (`F <hz>`, computed live from the TLE via Skyfield).
- Whichever satellite's flowgraph is currently running *polls* that same
  rigctld for the current frequency (`f`) via an embedded Python block
  (`rig_freq_poller_0`) and feeds it into a GRC variable (`freq`) via a
  `Message Pair to Variable` block.
- Because there's exactly one rigctld for the whole fleet and only one
  flowgraph is ever alive at a time (single SDR), there's no per-satellite
  port to manage - every `.grc` points at the same `127.0.0.1:<rig_port>`.
- This is genuine Hamlib, not a custom protocol, so a real copy of
  Gpredict's radio control window can point at the same address (as a NET
  rigctl rig) purely for a visual read-out if you want one.

**What actually happens to that `freq` variable once it updates** - this
is the part worth understanding fully, since it's easy to assume Doppler
correction means re-tuning the SDR hardware itself, and that's not what
happens here. Look at `osmosdr_source_0`'s own tuned frequency in any
working `.grc` (`geoscan1.grc` is the reference): its value is a fixed
formula, `nfreq - offset` - built only from `nfreq` (the satellite's
*nominal*, unchanging frequency) and `offset` (a fixed DC-avoidance
offset, `50e3` Hz by convention). **The SDR hardware is tuned exactly
once, at flowgraph startup, and never retuned for the rest of the
pass** - deliberately, since continuously re-tuning real hardware
mid-pass risks PLL relock glitches and settling delays at exactly the
moment you can least afford to lose samples. All the actual Doppler
tracking happens one stage later, in the Signal Source block
(`analog_sig_source_x_0_0`) feeding a `Multiply` block: its frequency is
`-(freq-nfreq+offset)+BFO` - it *does* reference the live `freq`
variable, and continuously recalculates the mixing frequency to shift
the signal by exactly the live Doppler offset, entirely in software,
after the fixed-frequency hardware has already captured it. As long as
the shifted signal stays inside the low-pass filter's passband (25 kHz
by convention here, comfortably wider than the few-kHz shift a typical
LEO pass produces at these frequencies), this works correctly and avoids
ever touching the hardware mid-pass at all.

**The one real pitfall, confirmed the hard way on a real satellite:**
`rig_freq_poller` is not the only block that can feed a `Message Pair to
Variable` block, and the other common one looks superficially similar
but does something completely different. `gpredict_doppler` is part of
the same OOT module and produces the same kind of message output - but
it's a *passive listener*, waiting for an actual instance of the
Gpredict application to connect to it and push frequency updates over
Gpredict's own native rig-control protocol. This toolkit never plays
that role - `run_passes.py` only ever pushes frequency *into* `rigctld`
as a Hamlib client; it never connects *out* as a Gpredict client to
anything. A flowgraph wired with `gpredict_doppler` instead of
`rig_freq_poller` will run with no errors, tune correctly at pass start,
and then silently receive zero Doppler correction for the entire pass -
the console just shows `[doppler] Waiting for connection on:
127.0.0.1:<port>` for the whole thing, since nothing ever will connect.
**Always use `rig_freq_poller`, matching `geoscan1.grc`'s wiring exactly**
- if a satellite's `.grc` was built by copying an unrelated gr-gpredict
example rather than an existing satellite in this fleet, check this
specifically before trusting a pass's Doppler correction.

## Antenna control

`run_passes.py` optionally drives a rotor through `rotctld`, the same
pattern as Doppler - one persistent client connection fed by whichever
satellite is active. Unlike `rigctld`, you start `rotctld` yourself, since
it needs your actual serial port and rotor model:
```
rotctld -m 607 -r /dev/ttyUSB2
```
(no `-t` needed - `rotctld` defaults to port 4533, matching `rot_port` in
`satellites.yaml`.) Start it before or any time up to the first pass - the
client reconnects automatically once it's up.

To run with no rotor at all, remove `rot_host`/`rot_port` from
`satellites.yaml`; `run_passes.py` prints a note and skips antenna control.

### Why lead-ahead targeting, not a simple threshold

Az/el updates use lead-ahead targeting, ported from a proven, working
implementation ([N6RFM/Gpredict_K4KDR_N6RFM](https://github.com/N6RFM/Gpredict_K4KDR_N6RFM)'s
own rotor controller), replacing an earlier, simpler design (still
preserved for reference in `test_rotor_throttle.py`, but no longer what's
actually deployed). The earlier design just sent the satellite's current
position whenever it had moved far enough *or* enough time had passed -
straightforward, but it means every command aims at a point that's
already slightly stale by the time it lands, since the satellite keeps
moving while the rotor is still travelling toward wherever it *was*.

The current design instead only sends a new position once the satellite
has drifted `rot_threshold_deg` from wherever the rotor was last
commanded, and when it does send, it's not the satellite's instantaneous
position but a *predicted future point* - found by binary-searching for
the furthest time (up to the pass's own LOS) where the satellite still
sits within `rot_threshold_deg` of its current position. This gives the
rotor a real destination to travel toward continuously, rather than
repeatedly redirecting it toward a target that's already behind reality.
There is no time-based forced send at all - a slow-moving stretch of a
pass can go a long time between commands, which is correct, not a gap.
See `find_lead_ahead_target()` and `maybe_update_rotor()` in
`run_passes.py` for the implementation.

**A real bug found and fixed during development, worth understanding**:
the trigger comparison uses `Rotctld.get_pos()` - the rotor's own live,
polled position - not the last value it was *commanded* to go to. This
isn't a stylistic choice; comparing against a stale commanded value
actively breaks the math. Each lead-ahead target is already sent
`rot_threshold_deg` ahead of wherever the satellite was at trigger time,
so if the *next* trigger also measures from that same stale, already-
ahead target, the satellite has to drift `rot_threshold_deg` past it
before triggering again - and the following lead-ahead search then adds
*another* full `rot_threshold_deg` on top. The two thresholds compound,
roughly doubling the real step size the rotor ends up taking between
commands, regardless of what `rot_threshold_deg` is actually set to.
Comparing against the rotor's real, live position breaks that
compounding, since the rotor's own physical progress toward the previous
target is accounted for rather than ignored.

### Two settings, two different purposes

- **`rot_threshold_deg`** (`satellites.yaml` top level, default `5.0` if
  unset) - how much the satellite has to drift before a new command is
  sent, in the trigger check described above. Smaller means more frequent,
  smaller corrections; larger means fewer, bigger jumps. This is a
  *tracking behavior* tuning knob - it doesn't change how fast the rotor
  can physically move, only how often it's told to.
- **`rot_max_deg_per_sec`** (`satellites.yaml` top level, no default -
  the feature is simply off if unset) - your rotor's actual, empirically
  measured maximum slew rate. This isn't consumed by `run_passes.py`'s
  own tracking loop at all (no software can make a rotor move faster than
  its physical maximum); instead, `plan_passes.py` uses it to warn you,
  *before you approve a pass*, when that pass's peak angular rate would
  exceed what your rotor can actually keep up with - a real, physical
  limit surfaced as advance information rather than something you
  discover mid-pass.

### Measuring your rotor's real capability

Don't guess at `rot_max_deg_per_sec` from watching a pass or reading a
spec sheet - measure it directly with `measure_rotor_speed.py`, which
does a clean, isolated point-to-point timed move (no lead-ahead
complexity involved) and reports an achieved rate with a suggested,
conservative value already margined in:
```
python3 measure_rotor_speed.py
```

**A real, hard-learned lesson from developing this tool**: its arrival
tolerance (`--tolerance`, default `1.0` degree) matters enormously, and
getting it wrong produces measurements that look meaningful but are
actually artifacts. Too tight a tolerance relative to your rotor's own
real position-reporting precision means it may *never* register as
"arrived" even after it's genuinely done moving - one real test rotor
sat at a persistent `1.0` degree offset indefinitely at a `0.5` degree
tolerance, producing wildly inconsistent, seemingly-random rate
measurements between repeated runs (`1.04` to `6.47` deg/sec on
supposedly identical moves) purely from tolerance alone, with no real
change in the rotor's actual behavior at all. Once the tolerance was
loosened to match the rotor's real ~1 degree precision limit, three
different move distances converged to consistent, trustworthy
measurements within about 4% of each other. If your own measurements
look inconsistent between runs, suspect the tolerance before suspecting
the rotor - and the tool's own summary now always records which
tolerance was used, precisely so a set of results is never separated
from the setting that produced them.

Once you have a trustworthy number, apply a safety margin (the tool
suggests one already, based on the *slowest* of several measured
distances, since a real pass involves sustained tracking much more like
a long test move than a short burst) and add it to `satellites.yaml`:
```yaml
rot_max_deg_per_sec: 4.5
```
`test_rotor_leadahead.py` is the complementary tool for verifying the
*tracking logic itself* (imports the real `Rotctld`/`maybe_update_rotor`
directly from `run_passes.py`) against synthetic, predictable motion,
independent of waiting for a real pass - useful for confirming the
trigger and lead-ahead behavior work as designed, separately from
measuring the rotor's raw physical limits.

