# groundtrack: Development Environment

[← back to README](../README.md)

This page is deliberately not a pinned "supported versions" list - a
hardcoded table like that goes stale the moment anyone upgrades
anything, and nothing here would notice or update it. Instead:

**What's actually on your machine always takes priority over anything
below.** The table further down is a historical data point - evidence
that some combination of versions worked at some point in past
development - not a requirement, not a recommendation, and not more
authoritative than your own working setup. If your versions differ and
everything runs clean, that's what matters; the table's only real use is
as a reference point if something breaks with no obvious cause.

## Check your own environment live

```
python3 doctor.py
```

The **Environment versions** section reports, right now, on the actual
machine you're running it on: Python, OS, GNU Radio, gr-satellites (if
installed), pyyaml, skyfield, and Hamlib. None of these are checked
against a required minimum - it's a snapshot for your own reference and
for anyone helping you debug something, not a pass/fail gate.

## What's actually been confirmed working, from real development

These are versions this project has genuinely been built and tested
against - not a claim that anything else won't work, just what's
actually been run:

| Component | Confirmed version |
|---|---|
| GNU Radio Companion (`grcc`) | 3.10.9.2 |
| Python | 3.12 |
| OS | Linux Mint 22.3 "Zena" (Ubuntu 24.04 "noble" base; apt-based, `libhamlib-utils` is the package name used in Setup) |
| Hamlib | `5.0.0~git 2026-07-01T19:29:58Z SHA=3bed166fd` (a git/development build, not a stable tagged release - worth knowing given the caveat below) |
| Hamlib rig model | `1` (Dummy backend, for Doppler via `rigctld`) |
| Hamlib rotor model | `607` |

## The one real version-sensitive assumption

Documented in full in
[Troubleshooting](troubleshooting.md#known-caveats): `rig_freq_poller`
(the embedded block every decode-and-relay and Doppler-tracked
recording-only flowgraph uses) sends a bare `f` command to `rigctld`'s
Dummy backend and expects a plain number back. That's confirmed working
with `5.0.0~git 2026-07-01T19:29:58Z SHA=3bed166fd` - worth noting this
is a git/development build, not a stable tagged release, which makes
this assumption somewhat more exposed than usual: a dev build can change
behavior between commits, not just between official releases. It's a
behavioral assumption either way, not something Hamlib's own
documentation formally guarantees identical across versions. If you
upgrade Hamlib and Doppler correction stops working with no other
obvious cause, this is the first thing worth re-testing - compare the
version `doctor.py` now reports before and after, and test with:

```
rigctld -m 1 -t 4531 &
rigctl -m 2 -r 127.0.0.1:4531 f
```

That should print a bare number. If it prints something else - extra
text, a different format - that's the assumption breaking, not a bug in
this toolkit's own code.

## Dependencies with no version pin at all

`gr-satellites`, Hamlib, and GNU Radio itself are all installed exactly
however your system provides them - there's no `requirements.txt`-style
pin forcing a specific release of any of them. `requirements.txt` in
this repo only covers the pure-Python dependencies (`pyyaml`,
`skyfield`) that pip can actually manage; everything else is a system
package this project assumes you've already set up per
[Setup](setup.md).
