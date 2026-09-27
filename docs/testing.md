# groundtrack: Testing

[← back to README](../README.md)

## The full end-to-end test recipe (no real pass, no test files required)

Everything below works with a freshly-set-up fleet - none of it requires a
satellite to have ever actually been received yet:

```
python3 doctor.py              # environment sanity: right folder? stray processes? free ports?
python3 preflight.py --live    # config correctness + each flowgraph launches without crashing
python3 relay.py &             # (or: nohup python3 relay.py > relay.log 2>&1 &)
python3 test_downstream.py     # pushes frames through the relay to your decoder(s)
```

If all four of those come back clean, every layer of the system - config,
flowgraph startup, relay connectivity, and decoder connectivity - has been
verified, and the only thing left untested is the live RF/SDR path itself,
which only a real pass (or `preflight.py --live`'s brief hardware launch)
touches.

## Sending test frames to your decoder (without waiting for a pass)

**`test_downstream.py` is the one to run — it works with zero setup, no test files required.** For every configured satellite it automatically checks whether a real captured `.kss` file exists yet; if so it replays real frames, and if not (the normal situation before a satellite's first real pass) it transparently falls back to synthetic frames instead. You never need to decide which mode to use or locate a file yourself:

```
python3 relay.py &
python3 test_downstream.py
```

That tests every satellite in `satellites.yaml` in one pass. To test just one:
```
python3 test_downstream.py --satellite GEOSCAN-1 --count 5
```

Point each satellite's decoder tab at its `consumer_port` (from `satellites.yaml`) before running this, and watch its history pane for new entries as frames arrive. Synthetic frames will very likely show up as CRC/parse errors in the decoder - that's expected, since the payload is garbage; what matters is that the frame *count* goes up at all, confirming the relay→decoder wire and KISS framing work correctly end to end.

Once a satellite has actually completed a real pass and has a non-empty `.kss` file on disk, `test_downstream.py` will automatically start using those real frames instead - no flag to remember, no path to look up.

**`send_test_frames.py` still exists underneath** for single-satellite, explicit control (e.g. forcing synthetic even if a real capture exists, or pointing at a specific file):
```
python3 send_test_frames.py --satellite GEOSCAN-2 --replay /home/YOUR_USERNAME/Desktop/GEOSCAN-2.kss --count 5
python3 send_test_frames.py --satellite GEOSCAN-2 --synthetic 5
```

## Verifying Doppler and rotor control are working

**From inside the script:** `python3 run_passes.py --verbose` prints a
line while a pass is active, by default every 5 seconds
(`--status-interval` controls this) - the Doppler-corrected frequency
updates every second regardless of this setting, but the rotor line
only actually changes when the satellite has drifted enough to warrant
a new command (lead-ahead targeting, not a fixed cadence - see
[Tracking Control](tracking-control.md)):
```
[GEOSCAN-2] el= 31.5 az=214.3  freq=436,158,412 Hz (doppler -1,588 Hz)
```
Nothing printing while a satellite should be up? Check `schedule.yaml` -
the pass may not be approved, or its window may not actually cover "now"
(stale schedule - rerun `plan_passes.py`).

**From outside the script**, independent of `run_passes.py` - useful to
confirm rigctld/rotctld are alive even with no pass active:
```
printf 'f\n' | nc 127.0.0.1 4532      # Doppler - current commanded freq
printf 'p\n' | nc 127.0.0.1 4533      # Rotor - current commanded az/el
```
or, with Hamlib's own clients:
```
rigctl -m 2 -r 127.0.0.1:4532 f
rotctl -m 2 -r 127.0.0.1:4533 p
```
Query the frequency twice a few seconds apart *during an active pass* -
it should change each time, since Doppler correction recomputes every
second. The rotor's reported position is different: it may *not* change
between two quick queries, even during an active pass, if the satellite
hasn't yet drifted past the lead-ahead threshold - a long quiet stretch
is correct, not broken. `test_rotor_leadahead.py` exercises this
directly against your real rotctld with synthetic, predictable motion
(no real pass needed) if you want to verify the rotor side thoroughly,
including watching the rotor's own physical display against periodic
independent position queries.
Outside a pass both will sit at whatever idle value the daemon started
with (rigctld's Dummy backend defaults to 145000000 Hz), which is
expected, not broken.

Be careful testing the rotor manually with a real `P <az> <el>` command -
unlike rigctld's Dummy backend, `rotctld` is driving real hardware and
will actually move the antenna.

"Connection refused" on either port means that daemon isn't running or
isn't listening where `satellites.yaml` says - check with
`ps aux | grep rigctld` / `ps aux | grep rotctld`.

**No pass due for hours and want to test sooner?** Temporarily lower one
satellite's `min_elev_deg` in `satellites.yaml` (e.g. to `0`), rerun
`plan_passes.py --hours 2`, and you'll likely get a near-term low pass to
watch trigger end-to-end. Remember to put the threshold back afterward.

