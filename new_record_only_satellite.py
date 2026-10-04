#!/usr/bin/env python3
"""
One command for the common case: a record-only satellite whose .grc is
identical to an existing one except for its name, frequency, and (not
present in the .grc at all) NORAD ID. Generates the new .grc from a
template, adds the satellite to satellites.yaml (as --record-only), and
compiles it - the three separate manual steps this exists to collapse.

This is a narrower, more deliberate version of the auto-generation
add_satellite.py used to do and stopped doing (see "Why nothing here
touches .grc files" in docs/adding-satellites.md) - the historical bugs
there (a decoder/relay block left present-but-unconfigured, an orphaned
kiss_encode_pdu block) were specific to the decode-and-relay shape, which
a record-only flowgraph doesn't have at all: no decoder, no relay block,
no KISS encoder. The only things that vary here are six already-identified
text fields (a flowgraph id/title, a recording filename prefix, a display
label, and one frequency value used in two places) - not the graph's
structure or its connections, which are copied byte-for-byte from the
template and never touched.

It refuses, writing nothing, if the template doesn't look like a record-only
flowgraph: its freq/nfreq values don't match each other (the normal case for
a brand new one sitting at the downlink frequency), or any of the six fields
doesn't appear exactly where expected. It never overwrites an existing
satellite's .grc.

Keeping a dedicated, untouched template (rather than pointing --template at
whatever satellite you used last) means later customizing one real
satellite's .grc can't silently change what every future one is built from.

Usage:
    cp flowgraphs/scionx.grc flowgraphs/_record_only_template.grc   # once
    python3 new_record_only_satellite.py --name NEWSAT-7 --norad 12345 \\
        --freq 437500000
    python3 new_record_only_satellite.py --name NEWSAT-7 --norad 12345 \\
        --freq 437500000 --dry-run
"""

import argparse
import os
import subprocess
import sys

import yaml

import station
from add_satellite import slugify
from doctor import check_stray_compiled_files

DEFAULT_TEMPLATE = "flowgraphs/_record_only_template.grc"
FALLBACK_TEMPLATE = "flowgraphs/scionx.grc"


class Refusal(Exception):
    """Something about the template isn't safe to substitute into
    mechanically - never a crash, always reported, nothing written."""


def discover_fields(text):
    """Reads the template (read-only) to find its own current id/title,
    basefile, waterfall name, and shared freq/nfreq value - these become
    the exact find-patterns for the text substitution, so this never
    depends on a hardcoded assumption about what the template contains."""
    grc = yaml.safe_load(text)
    blocks = {b["name"]: b for b in grc.get("blocks", [])}

    opt_id = grc.get("options", {}).get("parameters", {}).get("id")
    opt_title = grc.get("options", {}).get("parameters", {}).get("title")
    if not opt_id or opt_id != opt_title:
        raise Refusal(f"options.id ({opt_id!r}) and options.title "
                      f"({opt_title!r}) should match and be the template's "
                      f"own slug - this doesn't look like a clean template")

    sink = blocks.get("filerepeater_AdvFileSink_0")
    if sink is None:
        raise Refusal("no filerepeater_AdvFileSink_0 block - this doesn't "
                      "look like a record-only flowgraph")
    basefile = sink["parameters"].get("basefile")

    # The waterfall's display name is the one cosmetic field that follows the satellite. Its
    # block is normally qtgui_waterfall_sink_x_0, but a template saved from some other
    # flowgraph may name it differently - or have no waterfall at all, in which case there's
    # simply nothing to rename. (That last case used to go hunting for the literal text
    # 'name: None' and refuse with a message that said nothing about the real reason.)
    waterfall = blocks.get("qtgui_waterfall_sink_x_0")
    if waterfall is None:
        sinks = [b for b in grc.get("blocks", []) if b.get("id") == "qtgui_waterfall_sink_x"]
        if len(sinks) > 1:
            raise Refusal(f"{len(sinks)} waterfall displays ({', '.join(b['name'] for b in sinks)}) and "
                          f"none named qtgui_waterfall_sink_x_0 - can't tell which one's title should "
                          f"follow the satellite")
        waterfall = sinks[0] if sinks else None
    wf_name = waterfall["parameters"].get("name") if waterfall else None
    if wf_name in (None, "", '""', "''"):
        wf_name = None      # no waterfall, or one with no title: nothing to change

    freq_block, nfreq_block = blocks.get("freq"), blocks.get("nfreq")
    if freq_block is None or nfreq_block is None:
        raise Refusal("no freq and/or nfreq Parameter block - this doesn't "
                      "match the expected template shape")
    freq_val = freq_block["parameters"].get("value")
    nfreq_val = nfreq_block["parameters"].get("value")
    if freq_val != nfreq_val:
        raise Refusal(f"freq ({freq_val!r}) and nfreq ({nfreq_val!r}) differ "
                      f"in the template - expected them equal (a template "
                      f"sitting at its own placeholder downlink frequency), "
                      f"so there's no single safe value to replace")

    return {
        "id_title": opt_id,
        "basefile": basefile,
        "wf_name": wf_name,
        "freq_value": freq_val,
        "has_record_iq": "record_iq" in blocks,
    }


def build_new_text(text, old, new_slug, new_name, new_freq_hz):
    """Six finds, six replacements, each count-checked before anything is
    written - refuses (changing nothing) rather than guess if any count is
    off, since a silent partial substitution would be worse than stopping."""
    new_freq_str = str(new_freq_hz)
    # The frequency is written the way GRC wrote it: a value like 437.500e6 can't be an
    # integer, so it's plain; a plain integer such as 400500000 is quoted ('400500000').
    # Find it in whichever form the template actually uses, and write the new one in that
    # same form - searching for only the plain form made a perfectly good template refuse.
    freq_find, freq_replace = f"value: {old['freq_value']}", f"value: {new_freq_str}"
    for quote in ("'", '"'):
        quoted = f"value: {quote}{old['freq_value']}{quote}"
        if text.count(freq_find) != 2 and text.count(quoted) == 2:
            freq_find, freq_replace = quoted, f"value: {quote}{new_freq_str}{quote}"
    subs = [
        (f"    id: {old['id_title']}", f"    id: {new_slug}", 1),
        (f"    title: {old['id_title']}", f"    title: {new_slug}", 1),
        (f"basefile: {old['basefile']}", f"basefile: {new_slug}", 1),
    ]
    if old["wf_name"]:     # a template with no waterfall (or an untitled one) has no name to change
        subs.append((f"name: {old['wf_name']}", f"name: {new_name.upper()}", 1))
    subs.append((freq_find, freq_replace, 2))
    for find, _, expected in subs:
        got = text.count(find)
        if got != expected:
            raise Refusal(f"expected {expected} occurrence(s) of {find!r}, "
                          f"found {got} - refusing rather than guess")
    for find, replace, _ in subs:
        text = text.replace(find, replace)
    return text


def no_template_message():
    """One line on purpose: the GUI's Add dialog shows only the last line of a failure."""
    return (f"station {station.current()!r} has no record-only template: put a flowgraph you already run on "
            f"this radio at {DEFAULT_TEMPLATE} (deliberately not borrowed from another station - radios "
            f"differ in more than a device string)")


def main():
    import station
    station.enter()
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="e.g. NEWSAT-7")
    ap.add_argument("--norad", required=True, type=int,
                     help="not present in the .grc at all - used only in satellites.yaml, "
                          "for TLE lookup and pass prediction")
    ap.add_argument("--freq", required=True, type=int, help="downlink frequency in Hz")
    ap.add_argument("--min-elev", type=float, default=15.0)
    ap.add_argument("--template", default=None,
                     help=f"defaults to {DEFAULT_TEMPLATE} if it exists, else "
                          f"{FALLBACK_TEMPLATE} (with a suggestion to save a dedicated copy)")
    ap.add_argument("--record-iq-toggle", action="store_true",
                     help="pass through to add_satellite.py - only meaningful if the "
                          "template is already wired for it (this tool never wires a "
                          "template; see wire_record_iq.py for that)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, do nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    template = args.template
    if template is None:
        if os.path.exists(DEFAULT_TEMPLATE):
            template = DEFAULT_TEMPLATE
        elif station.current():
            # inside a station, scionx.grc (if it even exists here) is some OTHER radio's flowgraph
            sys.exit(no_template_message())
        else:
            template = FALLBACK_TEMPLATE
            print(f"NOTE: using {FALLBACK_TEMPLATE} directly, since {DEFAULT_TEMPLATE} "
                  f"doesn't exist yet. Consider saving a dedicated copy - editing "
                  f"{FALLBACK_TEMPLATE} later for its own satellite-specific reasons "
                  f"would then silently change what future satellites are built from:\n"
                  f"  cp {FALLBACK_TEMPLATE} {DEFAULT_TEMPLATE}\n")

    try:
        with open(template, newline="") as f:
            text = f.read()
    except FileNotFoundError:
        if station.current() and os.path.normpath(template) == os.path.normpath(DEFAULT_TEMPLATE):
            sys.exit(no_template_message())   # the GUI always passes --template explicitly
        sys.exit(f"Template not found: {template}")
    if "\r" in text:
        sys.exit(f"{template} has Windows line endings; expected the plain line "
                 f"endings GRC writes")

    try:
        fields = discover_fields(text)
    except Refusal as r:
        sys.exit(f"Refusing to use {template} as a template: {r}")

    slug = slugify(args.name)
    new_grc = f"flowgraphs/{slug}.grc"
    if os.path.exists(new_grc):
        sys.exit(f"{new_grc} already exists - this tool only creates new "
                 f"satellites, never overwrites one. Use GRC directly to edit it.")

    try:
        new_text = build_new_text(text, fields, slug, args.name, args.freq)
    except Refusal as r:
        sys.exit(f"Refusing to generate from {template}: {r}")

    if args.record_iq_toggle and not fields["has_record_iq"]:
        print(f"NOTE: --record-iq-toggle was given, but {template} isn't wired for "
              f"it - satellites.yaml will still get record_iq_toggle: true, but "
              f"you'll need to run wire_record_iq.py {args.name} afterward (same "
              f"as the template itself would need).")

    import difflib
    diff = difflib.unified_diff(text.split("\n"), new_text.split("\n"),
                                fromfile=template, tofile=new_grc, lineterm="", n=1)
    print(f"Will create {new_grc} from {template}:")
    print("\n".join("  " + line for line in diff))
    print(f"\nWill also run:")
    import station
    add_cmd = [sys.executable, station.script_path("add_satellite.py"), "--name", args.name,
               "--norad", str(args.norad), "--freq", str(args.freq),
               "--min-elev", str(args.min_elev), "--record-only"]
    if args.record_iq_toggle:
        add_cmd.append("--record-iq-toggle")
    print(f"  {' '.join(add_cmd)}")
    print(f"  grcc {new_grc}")

    if args.dry_run:
        print("\n(--dry-run: nothing written, nothing run)")
        return
    if not args.yes:
        try:
            ans = input("\nProceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("Not applied.")
            return

    with open(new_grc, "w", newline="") as f:
        f.write(new_text)
    print(f"\nwrote {new_grc}")

    # captured, not streamed: add_satellite.py's own "Still needed" checklist
    # assumes it's being run on its own (step 1 "build the .grc", step 2
    # "grcc it") - both already done here, so echoing it verbatim would be
    # actively misleading. Shown in full only if it fails, for debugging.
    result = subprocess.run(add_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"add_satellite.py failed (exit {result.returncode}):\n"
                 f"{result.stdout}{result.stderr}\n"
                 f"{new_grc} was written, but satellites.yaml was not updated. "
                 f"Fix the problem above and run add_satellite.py yourself, or "
                 f"delete {new_grc} and start over.")
    # a genuine warning (e.g. a duplicate NORAD) still needs to be seen, even
    # though the run succeeded - only the now-redundant "Still needed"
    # checklist is actually misleading in this context
    for line in result.stdout.splitlines():
        if line.startswith("NOTE:"):
            print(line)
    print(f"added to satellites.yaml")

    # -o flowgraphs/, matching regen_all.sh exactly: grcc otherwise writes
    # its .py to the CURRENT directory, not the .grc's own folder - a real,
    # repeated source of confusion elsewhere in this project (it's exactly
    # what doctor.py's "stray compiled flowgraphs" check exists to catch).
    # Telling it explicitly avoids the problem instead of cleaning up after it.
    result = subprocess.run(["grcc", "-o", os.path.dirname(new_grc), new_grc])
    if result.returncode != 0:
        sys.exit(f"grcc failed (exit {result.returncode}) - {new_grc} and the "
                 f"satellites.yaml entry both exist, but flowgraphs/{slug}.py "
                 f"was not produced. The entry is disabled by default, so "
                 f"nothing will try to launch it until that's fixed and "
                 f"./regen_all.sh (or grcc again) succeeds.")

    # a safety net on top of -o above: if anything grcc generated (an embedded
    # block's module - e.g. rig_freq_poller's - which the flowgraph imports at
    # launch) lands in cwd anyway, move it into flowgraphs/ beside the .py
    check_stray_compiled_files(fix=True)

    print(f"\nDone. {args.name} is in satellites.yaml as enabled: false. Next:")
    if args.record_iq_toggle and not fields["has_record_iq"]:
        print(f"  python3 wire_record_iq.py {args.name}")
        print(f"  ./regen_all.sh")
    print(f"  python3 preflight.py")
    print(f"  python3 toggle_satellite.py --enable {args.name}")


if __name__ == "__main__":
    main()
