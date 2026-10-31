#!/usr/bin/env python3
"""
Move satellites between stations or computers as one small file.

    python3 share_satellite.py export GBSAT UTNH-Sat --out helix_set.gtsat
    python3 share_satellite.py list helix_set.gtsat           # what is inside (changes nothing)
    python3 share_satellite.py import helix_set.gtsat         # on the other computer / station
    python3 share_satellite.py import helix_set.gtsat --replace   # overwrite satellites that already exist

A bundle (.gtsat, a zip) holds each satellite's satellites.yaml entry, its .grc flowgraph and, if it has
one, its custom TLE. Carry it any way you like (RustDesk file transfer, scp, USB, email) - GitHub is not involved.
It holds no coordinates, host names, IQ paths or SDR serial numbers.

What is NOT carried over from the other computer, and is kept as THIS station has it:
  - the SDR device string. The flowgraph's source block is set from this station's sdr.local.yaml
    (see set_sdr.py), so a different serial number or soapy=/bitpack= setting on this computer is used.
    With no entry for that driver the flowgraph gets no serial (it opens the first radio of that type)
    and you are told to run set_sdr.py.
  - enabled/priority. New satellites arrive DISABLED; review, then enable. A satellite you replace keeps
    its own enabled and priority.
  - ground station, rotor, ports of other satellites. Relay ports that clash with yours are renumbered.

Imports never overwrite silently: an existing satellite or flowgraph is skipped unless you answer
"replace" (asked on a terminal) or give --replace; the old files are kept as .bak-<time> copies.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone

import yaml

import passcheck
import sdr_local
from tle_number import catalog_number

FORMAT = 1
CONFIG = "satellites.yaml"
DROP_ON_EXPORT = ("enabled", "priority", "script")
ABS_PATH = re.compile(r"(?:/home/|/Users/)[^\s'\",)]+")


class ShareError(Exception):
    pass


def slugify(name):
    return name.lower().replace("-", "").replace(" ", "")


def load_cfg(folder):
    try:
        with open(os.path.join(folder, CONFIG)) as f:
            return yaml.safe_load(f) or {}
    except OSError:
        raise ShareError(f"no {CONFIG} in {folder}")


def save_cfg(folder, cfg):
    path = os.path.join(folder, CONFIG)
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with open(path, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)


def strip_serial(args):
    """Device string without its serial= part (the radio then isn't named; the importer sets its own)."""
    parts = [p for p in str(args).split(",") if not p.strip().startswith("serial=")]
    return ",".join(parts)


def set_source_args(text, new):
    """.grc text with its first osmosdr source's device string replaced; None when it can't be done safely."""
    old = sdr_local.grc_source_args(text)
    if not old:
        return None
    find = f"    args: {old}\n"
    if text.count(find) != 1:
        return None
    return text.replace(find, f"    args: {new}\n", 1)


def parse_tles(path):
    """{norad: [name, line1, line2]} from a 3-line TLE file ({} if unreadable)."""
    out = {}
    try:
        lines = [l.rstrip("\n") for l in open(path) if l.strip()]
    except OSError:
        return out
    for i, l in enumerate(lines):
        if l.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            n = catalog_number(l)
            if n is not None:
                name = lines[i - 1] if i > 0 and not lines[i - 1].startswith(("1 ", "2 ")) else ""
                out[n] = [name, l, lines[i + 1]]
    return out


def grc_rel(entry):
    g = passcheck.grc_text_for(entry)
    return g if g else f"flowgraphs/{slugify(entry['name'])}.grc"


# ------------------------------------------------------------------ export

def export_bundle(folder, names, out, station_name=""):
    cfg = load_cfg(folder)
    by_name = {str(s.get("name", "")).strip().lower(): s for s in cfg.get("satellites", []) or []}
    missing = [n for n in names if n.strip().lower() not in by_name]
    if missing:
        raise ShareError("not in this station's satellites.yaml: " + ", ".join(missing))
    custom = {}
    if cfg.get("custom_tle_file"):
        custom = parse_tles(os.path.normpath(os.path.join(folder, str(cfg["custom_tle_file"]))))
    manifest = {"format": FORMAT, "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source_station": station_name, "satellites": [], "custom_tle": []}
    files, warnings = {}, []
    for n in names:
        s = by_name[n.strip().lower()]
        rel = grc_rel(s)
        try:
            with open(os.path.join(folder, rel)) as f:
                text = f.read()
        except OSError:
            raise ShareError(f"{s['name']}: its flowgraph {rel} does not exist here - nothing to export")
        old = sdr_local.grc_source_args(text)
        if old and sdr_local.serial_of(old):
            text = set_source_args(text, strip_serial(old)) or text
        paths = sorted(set(ABS_PATH.findall(text)))
        if paths:
            warnings.append(f"{s['name']}: its flowgraph names path(s) on this computer that may not exist on the other: "
                            + ", ".join(paths[:4]) + (" ..." if len(paths) > 4 else ""))
        entry = {k: v for k, v in s.items() if k not in DROP_ON_EXPORT}
        arc = f"grc/{os.path.basename(rel)}"
        files[arc] = text
        manifest["satellites"].append({"entry": entry, "grc": arc})
        if s.get("norad") in custom and custom[s["norad"]] not in [c["lines"] for c in manifest["custom_tle"]]:
            manifest["custom_tle"].append({"norad": s["norad"], "lines": custom[s["norad"]]})
    manifest["warnings"] = warnings
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
        for arc, text in files.items():
            z.writestr(arc, text)
    return manifest


# ------------------------------------------------------------------ read

def read_bundle(path):
    try:
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format") != FORMAT:
                raise ShareError(f"{path} is bundle format {manifest.get('format')}; this tool reads format {FORMAT} - update both computers")
            grcs = {}
            for item in manifest.get("satellites", []):
                arc = item.get("grc", "")
                if not re.fullmatch(r"grc/[\w.\-]+\.grc", arc):
                    raise ShareError(f"unsafe file name in bundle: {arc!r}")
                grcs[arc] = z.read(arc).decode()
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as e:
        raise ShareError(f"cannot read {path} as a satellite bundle: {e}")
    return manifest, grcs


def describe(manifest):
    lines = [f"Bundle made {manifest.get('created', '?')} from station '{manifest.get('source_station') or '?'}':"]
    for it in manifest["satellites"]:
        e = it["entry"]
        lines.append(f"  {e.get('name')}  NORAD {e.get('norad')}  {e.get('freq_hz')} Hz  flowgraph {os.path.basename(it['grc'])}"
                     + ("  (relay ports)" if "producer_port" in e else "  (record only)"))
    for c in manifest.get("custom_tle", []):
        lines.append(f"  custom TLE for NORAD {c['norad']}")
    for w in manifest.get("warnings", []):
        lines.append(f"  note from export: {w}")
    return "\n".join(lines)


# ------------------------------------------------------------------ import

def import_bundle(folder, path, decide, compile_grc=True, say=print):
    """decide(kind, name) -> 'skip' or 'replace' is asked for each clash. Returns [(name, 'imported'|'replaced'|'skipped')]."""
    manifest, grcs = read_bundle(path)
    cfg = load_cfg(folder)
    sats = cfg.setdefault("satellites", [])
    local = sdr_local.load_local(os.path.join(folder, sdr_local.LOCAL_FILE))
    os.makedirs(os.path.join(folder, "flowgraphs"), exist_ok=True)
    results, written, touched = [], [], set()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for item in manifest["satellites"]:
        entry = dict(item["entry"])
        name = str(entry.get("name", "")).strip()
        if not name:
            say("  skipped an entry with no name")
            continue
        text = grcs[item["grc"]]
        rel = f"flowgraphs/{slugify(name)}.grc"
        entry["script"] = f"flowgraphs/{slugify(name)}.py"
        idx = next((i for i, s in enumerate(sats) if str(s.get("name", "")).strip().lower() == name.lower()), None)
        exists_grc = os.path.exists(os.path.join(folder, rel))
        replacing = False
        if idx is not None or exists_grc:
            what = "is already in satellites.yaml" if idx is not None else f"{rel} already exists"
            if decide(what, name) != "replace":
                say(f"  [{name}] {what} - skipped (yours is kept)")
                results.append((name, "skipped"))
                continue
            replacing = True
        for s in sats:
            if s is not (sats[idx] if idx is not None else None) and s.get("norad") == entry.get("norad"):
                say(f"  [{name}] note: NORAD {entry.get('norad')} is also used by {s.get('name')} here - enable only one of them per pass")
        # the SDR: this station's own device string
        old = sdr_local.grc_source_args(text)
        if old:
            new = local.get(sdr_local.driver_of(old) or "")
            if new:
                swapped = set_source_args(text, new)
                if swapped:
                    text = swapped
                    say(f"  [{name}] SDR device set from this station's {sdr_local.LOCAL_FILE}: {new}")
                else:
                    say(f"  [{name}] could not set the SDR device automatically - check the source block in {rel}")
            else:
                say(f"  [{name}] no device string for driver '{sdr_local.driver_of(old)}' in {sdr_local.LOCAL_FILE} - "
                    f"the flowgraph will open the first such radio. Run: python3 set_sdr.py --from flowgraphs/<a working flowgraph>.grc")
        # relay ports that clash with this station's
        for key in ("producer_port", "consumer_port"):
            if key in entry:
                used = {s.get(key) for i, s in enumerate(sats) if i != idx}
                if entry[key] in used:
                    newp = max([p for p in used if isinstance(p, int)] + [entry[key]]) + 1
                    if key == "producer_port":
                        pat = re.compile(r"(port['\"]?:\s*['\"]?)" + str(entry[key]) + r"(?!\d)")
                        if pat.search(text):
                            text = pat.sub(lambda m: m.group(1) + str(newp), text)
                        else:
                            say(f"  [{name}] {key} {entry[key]} is taken here; set {newp} in the flowgraph's socket block by hand")
                    say(f"  [{name}] {key} {entry[key]} is already used here - renumbered to {newp}")
                    entry[key] = newp
        # write the flowgraph, keeping what was there
        full = os.path.join(folder, rel)
        if replacing and exists_grc:
            shutil.copy2(full, f"{full}.bak-{stamp}")
        with open(full, "w") as f:
            f.write(text)
        written.append(rel)
        touched.add(entry.get("norad"))
        if replacing and idx is not None:
            for k in ("enabled", "priority"):
                if k in sats[idx]:
                    entry[k] = sats[idx][k]
                else:
                    entry.pop(k, None)
            entry.setdefault("enabled", False)
            sats[idx] = entry
            results.append((name, "replaced"))
        else:
            entry["enabled"] = False
            sats.append(entry)
            results.append((name, "imported"))
        paths = sorted(set(ABS_PATH.findall(text)))
        if paths:
            say(f"  [{name}] check these paths in {rel}, they came from the other computer: " + ", ".join(paths[:4]))
    if any(r[1] != "skipped" for r in results):
        save_cfg(folder, cfg)
    # custom TLEs
    wanted = [c for c in manifest.get("custom_tle", []) if c["norad"] in touched]
    if wanted:
        cpath = cfg.get("custom_tle_file")
        if not cpath:
            say("  This station has no custom_tle_file set. Add a line such as  custom_tle_file: ../tle/custom.txt  to "
                f"{CONFIG}, then add these lines to that file:")
            for c in wanted:
                say("      " + "\n      ".join(c["lines"]))
        else:
            cfull = os.path.normpath(os.path.join(folder, str(cpath)))
            have = parse_tles(cfull)
            for c in wanted:
                if c["norad"] in have:
                    say(f"  custom TLE for NORAD {c['norad']} already exists here - kept yours")
                    continue
                os.makedirs(os.path.dirname(cfull), exist_ok=True)
                with open(cfull, "a") as f:
                    f.write(("\n" if os.path.exists(cfull) and os.path.getsize(cfull) else "") + "\n".join(c["lines"]) + "\n")
                say(f"  custom TLE for NORAD {c['norad']} added to {cpath}")
    if compile_grc and written:
        if shutil.which("grcc"):
            for rel in written:
                r = subprocess.run(["grcc", "-o", "flowgraphs", rel], cwd=folder)
                say(f"  compiled {rel}" if r.returncode == 0 else f"  grcc FAILED for {rel} - open it in GRC and look")
        else:
            say("  grcc was not found - compile each flowgraph: grcc -o flowgraphs flowgraphs/<name>.grc  (or ./regen_all.sh)")
    return results


# ------------------------------------------------------------------ command line

def main():
    orig = os.getcwd()
    if not (len(sys.argv) > 1 and sys.argv[1] == "list"):
        import station
        label = station.enter() or ""
    else:
        label = ""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write a bundle of satellites")
    e.add_argument("names", nargs="*", help="satellite names (as in satellites.yaml)")
    e.add_argument("--all", action="store_true", help="every satellite of this station")
    e.add_argument("--out", help="bundle file to write (default: ./<name>.gtsat)")
    l = sub.add_parser("list", help="show what is in a bundle")
    l.add_argument("file")
    i = sub.add_parser("import", help="add a bundle's satellites to this station")
    i.add_argument("file")
    i.add_argument("--replace", action="store_true", help="overwrite satellites/flowgraphs that already exist (old ones kept as .bak-)")
    i.add_argument("--no-compile", action="store_true", help="do not run grcc")
    args = ap.parse_args()
    here = os.getcwd()
    try:
        if args.cmd == "list":
            print(describe(read_bundle(os.path.join(orig, args.file))[0]))
            return
        if args.cmd == "export":
            names = args.names
            if args.all:
                names = [s["name"] for s in load_cfg(here).get("satellites", []) or []]
            if not names:
                sats = load_cfg(here).get("satellites", []) or []
                print(f"Satellites at this station{(' (' + label + ')') if label else ''} - name them on the command line, or use --all:\n")
                for s in sats:
                    print(f"  {str(s.get('name')):<22} NORAD {str(s.get('norad')):<7} {s.get('freq_hz')} Hz  "
                          f"{'enabled' if s.get('enabled', True) else 'disabled'}")
                print(f"\nExample:  python3 share_satellite.py export {sats[0]['name'] if sats else 'NAME'} --out mine.gtsat")
                return
            out = os.path.join(orig, args.out) if args.out else os.path.join(orig, (slugify(names[0]) if len(names) == 1 else "satellites") + ".gtsat")
            man = export_bundle(here, names, out, label)
            print(describe(man))
            print(f"\nWrote {out}\nCopy it to the other computer and run: python3 share_satellite.py import {os.path.basename(out)}")
            return
        path = os.path.join(orig, args.file)
        man = read_bundle(path)[0]
        print(describe(man) + "\n")

        def decide(what, name):
            if args.replace:
                return "replace"
            if sys.stdin.isatty():
                ans = input(f"  {name} {what}. Replace it with the bundle's version? (your old files are kept as .bak) [y/N]: ")
                return "replace" if ans.strip().lower() in ("y", "yes") else "skip"
            return "skip"
        res = import_bundle(here, path, decide, compile_grc=not args.no_compile)
        done = [n for n, r in res if r != "skipped"]
        print(f"\n{len(done)} of {len(res)} satellite(s) added or replaced." + (" They are DISABLED." if any(r == 'imported' for _, r in res) else ""))
        if done:
            print("Next: python3 preflight.py   then   python3 toggle_satellite.py --enable \"NAME\"   (GUI: Enable selected)")
    except ShareError as err:
        sys.exit(str(err))


if __name__ == "__main__":
    main()
