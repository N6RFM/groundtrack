# groundtrack

Unattended GNU Radio ground station automation for tracking multiple
satellites on a single shared SDR and rotor. Originally built for the
GEOSCAN cubesat constellation; now satellite-agnostic - any mix of
decode-and-relay satellites (a live downstream decoder receives
continuously-updated telemetry frames during each pass, via
[gr-satellites](https://github.com/daniestevez/gr-satellites) and a
small relay process that keeps the connection open across passes) and
recording-only satellites (raw IQ capture, no decoder required) can
share the same schedule, the same hardware, and the same toolkit. Also
uses Hamlib and Skyfield.

Point it at your ground station's coordinates and SDR, and it predicts
upcoming passes for review/approval, then for each approved pass:
retunes for live Doppler shift via a Hamlib `rigctld`, points an antenna
rotor via `rotctld`, records an IQ file, and - for satellites configured
with a downstream decoder - forwards decoded KISS frames through a relay
that stays connected across every pass - no manual tuning, no
re-clicking "Engage" in Gpredict, no reconnecting your decoder every
AOS. Satellites with no decoder yet just record raw IQ for later
analysis; nothing else in the toolkit requires one.

**What it assumes you already have:** GNU Radio 3.10+ with an SDR
supported by `gr-osmosdr` (developed against an Airspy), `gr-satellites`
installed (only needed for satellites that decode - recording-only
satellites don't need it), Hamlib (`rigctld`/`rotctld`), Python 3 with
`skyfield` and `pyyaml`, and (optionally) a Hamlib-compatible antenna
rotor and its serial interface.

**What it doesn't do:** demodulation/decoding itself (that's
`gr-satellites`, for satellites that use it), or drive a rotor/radio
that Hamlib doesn't already support.

## Quick start

```
git clone git@github.com:n6rfm/groundtrack.git
cd groundtrack
cp satellites.example.yaml satellites.yaml
python3 doctor.py
```

See [Setup](docs/setup.md) for the full walkthrough, and
[Troubleshooting](docs/troubleshooting.md) if something doesn't come
back clean.

## Documentation

This README is deliberately short - everything else lives in `docs/`,
split by what you're actually trying to do:

| Doc | What's in it |
|---|---|
| [Setup](docs/setup.md) | One-time setup, from a fresh clone to a passing `doctor.py` |
| [Daily Workflow](docs/daily-workflow.md) | The actual session-to-session routine: TLE refresh, preflight, planning, execution |
| [Adding a Satellite](docs/adding-satellites.md) | Decode-and-relay vs. recording-only vs. `extra_outputs` (a satellite output that bypasses the relay and connects straight to its own downstream app), editing, enabling/disabling |
| [Architecture](docs/architecture.md) | How the pieces fit together, `kiss_encode_pdu`, folder layout |
| [Relay and Bridging](docs/relay-and-bridging.md) | `relay.py`, `tcp_bridge.py`, which one a satellite actually uses, port conventions |
| [Stations](docs/stations.md) | More than one radio/antenna system: a folder per station, the GUI's station switch, running them side by side, sharing one TLE file |
| [Tracking Control](docs/tracking-control.md) | Doppler control, antenna/rotor control, lead-ahead targeting, measuring and configuring your rotor's real limits |
| [Scripts Reference](docs/scripts-reference.md) | What every script does, including `doctor.py` in depth |
| [Testing](docs/testing.md) | The full end-to-end test recipe, sending test frames, verifying Doppler/rotor without a real pass |
| [Troubleshooting](docs/troubleshooting.md) | Common problems and known caveats |
| [Environment](docs/environment.md) | What's actually confirmed working, and how to check your own versions live |
| [GUI.md](GUI.md) | The optional `groundtrack_gui.py` interface over these same CLI tools |

## GUI (optional)

`groundtrack_gui.py` gives you a table of every satellite plus buttons
for the actions documented above - add/edit/enable/disable/delete, run
checks, refresh TLEs, plan and execute passes. It's entirely optional:
every action is a real subprocess call to the exact same scripts and
flags documented here, so the CLI tools work identically whether or not
you ever open the GUI. See [GUI.md](GUI.md) for the full picture,
including why a few buttons open their own terminal window instead of
running inline, and how to manage `extra_outputs` (a satellite's
second output, bypassing the relay - see [Adding a
Satellite](docs/adding-satellites.md)) through the Edit dialog rather
than by hand-editing YAML.

```
python3 groundtrack_gui.py
```

## Authors

- N6RFM
- Claude (Anthropic)

## License

MIT - see LICENSE.
