#!/usr/bin/env python3
"""
This computer's own SDR device strings, kept out of git.

A flowgraph's SDR block holds a device string such as
    driver=airspy,soapy=0,serial=10a862dc3464c763
and the committed templates carry whatever the computer they were written on uses. Two
computers never share a serial number, so a satellite built from a committed template would open
the wrong radio (or none). Instead of editing templates by hand - and fighting git over them -
each station folder can hold an untracked file, sdr.local.yaml, with this computer's device
string per SDR driver:

    airspy: driver=airspy,soapy=0,serial=10a862dc3464c763
    rtlsdr: driver=rtlsdr,serial=00000002

new_record_only_satellite.py then writes that string into every new flowgraph it builds
(whatever the template says), and preflight.py warns when a flowgraph disagrees with it.
Create or update the file with set_sdr.py.
"""

import os
import re

import yaml

LOCAL_FILE = "sdr.local.yaml"            # in the station folder (the current directory)


def driver_of(args):
    m = re.search(r"driver=(\w+)", str(args or ""))
    return m.group(1) if m else None


def grc_source_args(text):
    """The device string of the first osmosdr source block in a .grc's text, or None."""
    try:
        grc = yaml.safe_load(text)
        for b in grc.get("blocks", []):
            if str(b.get("id", "")).startswith("osmosdr_source"):
                args = (b.get("parameters") or {}).get("args")
                if args:
                    return str(args)
    except Exception:
        pass
    return None


def serial_of(args):
    m = re.search(r"serial=([^,\s]+)", str(args or ""))
    return m.group(1) if m else None


def same_device(a, b):
    """True when two device strings open the same radio. With a serial number on both, that is the
    driver and serial (side options such as soapy=0 or bitpack=false don't change which radio it is);
    otherwise the strings must be identical."""
    if serial_of(a) and serial_of(b):
        return driver_of(a) == driver_of(b) and serial_of(a) == serial_of(b)
    return str(a) == str(b)


def load_local(path=LOCAL_FILE):
    """{driver: device string}; empty if the file doesn't exist or can't be read."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}


def save_local(mapping, path=LOCAL_FILE):
    with open(path, "w") as f:
        f.write("# This computer's SDR device strings, one per driver. Not committed (see .gitignore).\n"
                "# new_record_only_satellite.py puts these into new flowgraphs; preflight.py checks against them.\n"
                "# Change with: python3 set_sdr.py --from flowgraphs/<a working flowgraph>.grc\n")
        yaml.safe_dump(dict(sorted(mapping.items())), f, default_flow_style=False, sort_keys=False)


def apply_local(text):
    """(new text, note): the .grc text with its source block's device string replaced by this
    computer's, when sdr.local.yaml has one for that driver. Unchanged (note None) when there
    is no local file, no entry for the driver, nothing to change, or the line isn't found exactly once."""
    old = grc_source_args(text)
    if not old:
        return text, None
    new = load_local().get(driver_of(old) or "")
    if not new or same_device(new, old):
        return text, None
    find = f"    args: {old}\n"
    if text.count(find) != 1:
        return text, None
    return text.replace(find, f"    args: {new}\n", 1), f"SDR device set from {LOCAL_FILE}: {new} (the template said {old})"
