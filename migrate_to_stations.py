#!/usr/bin/env python3
"""
One-time move from the classic single-folder layout to multi-station mode.

Your whole current fleet becomes the first station (say r2, the beam), and a
second, empty station (say mini) is created beside it:

    before                          after
    ------                          -----
    satellites.yaml                 radios.yaml            (new)
    schedule.yaml                   r2/satellites.yaml     (moved)
    pass_log.jsonl                  r2/schedule.yaml       (moved)
    flowgraphs/                     r2/pass_log.jsonl      (moved)
    tle/                            r2/flowgraphs/         (moved - git mv, history kept)
    *.py (the scripts)              mini/satellites.yaml   (new skeleton)
                                    mini/flowgraphs/       (new, empty)
                                    tle/                   (stays: shared by every station)
                                    *.py                   (stay: shared, never duplicated)

The one edit made to a moved file: tle_file / custom_tle_file in the first
station's satellites.yaml, if relative, get a "../" prefix - the shared TLE
folder is now one level up. That's a plain line edit, so your comments and
formatting survive. Nothing else in it changes: script: paths, ports and
decoder paths all still resolve, because the station folder is laid out
exactly like the old project root.

It will NOT touch: run_passes.py or relay.py while running (it refuses -
stop them first), loose logs, recordings, or anything it doesn't recognise.

Safe to try: --dry-run shows exactly what would happen and changes nothing.
satellites.yaml, schedule.yaml and pass_log.jsonl - the files git doesn't
track, so the ones that can't be recovered from it - are copied to
.pre_stations_backup/ first. flowgraphs/ is moved whole rather than copied
(its tracked files are safe in git, and a move keeps everything in it,
compiled and untracked files included). If any step fails, the ones already
done are undone, so you're never left half-moved.

Usage:
    python3 migrate_to_stations.py --second-rig-port 4534 --dry-run
    python3 migrate_to_stations.py --second-rig-port 4534
    python3 migrate_to_stations.py --second-rig-port 4534 \\
        --first-label "R2 + beam (Az/El)" --second-label "Mini + helix (fixed)" \\
        --second-template ~/my_working_mini_flowgraph.grc

--second-template installs a flowgraph you already run on the second radio as
that station's _record_only_template.grc. It is deliberately not generated
from the first station's: the two radios differ in more than a device string
(the sample rates they offer, and so the decimation and filter settings that
follow from them), so a copy of the other radio's flowgraph with one line
swapped would look right and not work. Without it, the second station's
flowgraphs/ is left empty for you to fill.
"""

import argparse
import errno
import os
import re
import shutil
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
LOOSE_FILES = ["satellites.yaml", "schedule.yaml", "pass_log.jsonl"]
BACKUP_DIR = ".pre_stations_backup"
CARRIED_KEYS = ["ground_station", "tle_url", "notify"]   # copied into the new station as-is
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class MigrationError(Exception):
    pass


def p(*parts):
    return os.path.join(HERE, *parts)


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)


def uses_git():
    """True if flowgraphs/ has tracked files - then it must be moved with
    git mv, so history follows the files instead of showing delete + add."""
    try:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return False
        return bool(git("ls-files", "--", "flowgraphs").stdout.strip())
    except FileNotFoundError:
        return False


def rebase_path_lines(text):
    """tle_file / custom_tle_file, when relative, get ../ - done as a plain
    line edit so comments and formatting survive. Returns (new_text,
    [(key, old, new)]). Absolute and ~ paths are left alone."""
    changes = []

    def repl(m):
        key, value_text, comment = m.group(1), m.group(2).strip(), m.group(3) or ""
        quote = value_text[0] if value_text[:1] in "'\"" and value_text[-1:] == value_text[:1] else ""
        old = value_text[1:-1] if quote else value_text
        if not old or os.path.isabs(old) or old.startswith("~"):
            return m.group(0)
        new = os.path.normpath(os.path.join("..", old))
        changes.append((key, old, new))
        return f"{key}: {quote}{new}{quote}{comment}"

    new_text = re.sub(r"^(tle_file|custom_tle_file):[ \t]*(.*?)([ \t]+#.*)?$", repl, text, flags=re.M)
    return new_text, changes


def mini_skeleton(first_cfg, rig_port, first_name, second_name, rebased):
    """satellites.yaml for the new station: only what's genuinely common
    (ground station, shared TLE file) carried across, its own rig_port, and
    deliberately no rot_host/rot_port."""
    lines = [
        f"# Station '{second_name}' - see docs/stations.md.",
        f"# Everything for this radio lives in this folder. It differs from '{first_name}'",
        "# in its own rig_port below, and in having NO rot_host/rot_port at all - so",
        "# run_passes.py never steers an antenna for it. Add satellites with the GUI's",
        "# 'Add satellite...' (or add_satellite.py --radio ...).",
        "",
    ]
    for key in CARRIED_KEYS:
        if key in first_cfg:
            lines.append(yaml.safe_dump({key: first_cfg[key]}, sort_keys=False).rstrip())
    for key in ("tle_file", "custom_tle_file"):
        if key in first_cfg:
            lines.append(f"{key}: {rebased.get(key, first_cfg[key])}"
                         + ("   # shared by every station" if key == "tle_file" else ""))
    lines += [f"rig_port: {rig_port}", "", "satellites: []", ""]
    return "\n".join(lines)


def build_plan(a):
    """Everything decided and checked up front. Returns (plan, problems) -
    all the problems at once, so there's one round of fixing, not several."""
    problems = []
    first, second = a.first, a.second
    plan = {"first": first, "second": second}

    for label, name in (("--first", first), ("--second", second)):
        if not NAME_OK.match(name):
            problems.append(f"{label} {name!r}: use letters, digits, - or _ (it becomes a folder name)")
    if first.lower() == second.lower():
        problems.append("--first and --second must be different")

    if os.path.exists(p("radios.yaml")):
        problems.append("radios.yaml already exists - this is already in multi-station mode "
                        "(nothing to migrate)")
    if not os.path.exists(p("satellites.yaml")):
        problems.append("no satellites.yaml here - run this from the project folder, "
                        "the one holding the scripts")
    for d in (first, second):
        if os.path.exists(p(d)):
            problems.append(f"{d}/ already exists - refusing to merge into or overwrite it")
    if not os.path.isdir(p("flowgraphs")):
        problems.append("no flowgraphs/ folder here to move")

    # a run in progress holds files this is about to move
    try:
        with open(p("run_passes.lock")) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            problems.append(f"run_passes.py is running (PID {pid}) - stop it first")
        except OSError as e:
            if e.errno == errno.EPERM:  # alive, just someone else's
                problems.append(f"run_passes.py is running (PID {pid}) - stop it first")
            else:
                plan["stale_lock"] = True
    except (OSError, ValueError):
        pass

    # compiled flowgraphs that landed in the project root (grcc writes to the
    # current directory): after the move, nothing would look for them there
    strays = []
    for grc in sorted(os.listdir(p("flowgraphs"))) if os.path.isdir(p("flowgraphs")) else []:
        if grc.endswith(".grc"):
            stem = grc[:-4]
            strays += [f for f in os.listdir(HERE)
                       if f == f"{stem}.py" or (f.startswith(f"{stem}_") and f.endswith(".py"))]
    if strays:
        problems.append("compiled flowgraphs are sitting in the project root "
                        f"({', '.join(strays[:4])}{'...' if len(strays) > 4 else ''}) - "
                        "run  python3 doctor.py --fix  first, to move them where they belong")

    first_cfg, first_text = {}, ""
    if os.path.exists(p("satellites.yaml")):
        try:
            first_text = open(p("satellites.yaml")).read()
            first_cfg = yaml.safe_load(first_text) or {}
        except yaml.YAMLError as e:
            problems.append(f"satellites.yaml isn't valid YAML: {e}")
    if str(first_cfg.get("rig_port")) == str(a.second_rig_port):
        problems.append(f"--second-rig-port {a.second_rig_port} is the same as {first}'s - two "
                        "run_passes.py would both start rigctld on it")

    new_text, changes = rebase_path_lines(first_text)
    for key, old, new in changes:
        if os.path.abspath(p(old)) != os.path.abspath(p(first, new)):   # same file before and after?
            problems.append(f"{key}: {old!r} would not resolve to the same file as {new!r} after the move")
    use_git = uses_git()
    if use_git:
        # git mv refuses a folder holding a tracked file that's been deleted locally but not yet
        # committed ("bad source") - say so now, with the way out, not as a failure half-way through
        gone = [x for x in git("ls-files", "-d", "--", "flowgraphs").stdout.split("\n") if x]
        if gone:
            problems.append(f"{len(gone)} tracked file(s) in flowgraphs/ are deleted on disk but not committed "
                            f"({', '.join(gone[:3])}{'...' if len(gone) > 3 else ''}) and git can't move a folder "
                            f"holding them - either commit the deletion (git rm {gone[0]}) or bring the file back "
                            f"(git checkout -- {gone[0]}), then run this again")
    plan.update(first_cfg=first_cfg, first_text=first_text, new_first_text=new_text, changes=changes,
                rebased={k: n for k, _o, n in changes}, loose=[f for f in LOOSE_FILES if os.path.exists(p(f))],
                use_git=use_git)

    if a.second_template:
        import new_record_only_satellite as nros   # outside the try: an ImportError is a bug, not a bad template
        try:
            with open(os.path.expanduser(a.second_template), newline="") as f:
                text = f.read()
            fields = nros.discover_fields(text)
            # not just "looks like a template": prove the substitution that new_record_only_satellite.py
            # will do on it actually goes through, so a template that would be refused at first use is
            # refused now, while it's cheap to fix
            nros.build_new_text(text, fields, "probe", "PROBE", 437000000)
            plan["template_found"] = fields
            plan["template_path"] = os.path.expanduser(a.second_template)
        except FileNotFoundError:
            problems.append(f"--second-template {a.second_template}: no such file")
        except nros.Refusal as r:
            problems.append(f"--second-template {a.second_template} can't be used as a template: {r}")
        except Exception as e:  # unparseable YAML etc.
            problems.append(f"--second-template {a.second_template}: {e}")
    return plan, problems


def describe(plan, a):
    f, s = plan["first"], plan["second"]
    out = [f"Station '{f}' (your current fleet) and a new, empty station '{s}':", ""]
    out.append(f"  mkdir {f}/")
    out.append(f"  {'git mv' if plan['use_git'] else 'mv'} flowgraphs -> {f}/flowgraphs"
               + ("   (history follows the files)" if plan["use_git"] else ""))
    for name in plan["loose"]:
        out.append(f"  mv {name} -> {f}/{name}")
    for key, old, new in plan["changes"]:
        out.append(f"  {f}/satellites.yaml: {key}: {old}  ->  {new}")
    out.append(f"  create {s}/satellites.yaml  (rig_port {a.second_rig_port}, no rotor)")
    out.append(f"  create {s}/flowgraphs/")
    if plan.get("template_path"):
        t = plan["template_found"]
        out.append(f"  install {plan['template_path']} -> {s}/flowgraphs/_record_only_template.grc")
        out.append(f"      (compatible: flowgraph id {t['id_title']!r}, frequency {t['freq_value']})")
    out.append(f"  create radios.yaml  (default: {f})")
    out.append(f"  copy {', '.join(plan['loose'])} to {BACKUP_DIR}/ first (the files git doesn't track)")
    out.append("  tle/ and every script stay where they are")
    if plan.get("stale_lock"):
        out.append("  remove the stale run_passes.lock (its process is gone)")
    return "\n".join(out)


def execute(plan, a):
    """Does it, undoing completed steps in reverse if any step fails."""
    f, s = plan["first"], plan["second"]
    undo = []

    def restore_first_yaml():
        with open(p(f, "satellites.yaml"), "w") as fh:
            fh.write(plan["first_text"])

    def move(src, dst, use_git=False):
        if use_git:
            r = git("mv", src, dst)
            if r.returncode != 0:
                raise MigrationError(f"git mv {src} {dst} failed: {r.stderr.strip()}")
            return lambda: git("mv", dst, src)
        shutil.move(p(src), p(dst))
        return lambda: shutil.move(p(dst), p(src))

    try:
        os.makedirs(p(BACKUP_DIR), exist_ok=True)
        for name in plan["loose"]:
            shutil.copy2(p(name), p(BACKUP_DIR, name))

        os.mkdir(p(f))
        undo.append(lambda: os.rmdir(p(f)))

        back = move("flowgraphs", f"{f}/flowgraphs", plan["use_git"])
        undo.append(back)
        for name in plan["loose"]:
            undo.append(move(name, f"{f}/{name}"))

        if plan["changes"]:
            with open(p(f, "satellites.yaml"), "w") as fh:
                fh.write(plan["new_first_text"])
            undo.append(restore_first_yaml)

        os.makedirs(p(s, "flowgraphs"))
        undo.append(lambda: shutil.rmtree(p(s), ignore_errors=True))
        open(p(s, "flowgraphs", ".gitkeep"), "w").close()   # git doesn't track empty folders
        skeleton = mini_skeleton(plan["first_cfg"], a.second_rig_port, f, s, plan["rebased"])
        try:
            parsed = yaml.safe_load(skeleton)
        except yaml.YAMLError as e:
            raise MigrationError(f"the generated {s}/satellites.yaml wouldn't parse: {e}")
        if parsed.get("rig_port") != a.second_rig_port or parsed.get("satellites") != []:
            raise MigrationError(f"the generated {s}/satellites.yaml doesn't say what was intended")
        with open(p(s, "satellites.yaml"), "w") as fh:
            fh.write(skeleton)
        if plan.get("template_path"):
            shutil.copy(plan["template_path"], p(s, "flowgraphs", "_record_only_template.grc"))

        if plan.get("stale_lock"):
            os.remove(p("run_passes.lock"))

        def label_line(text):   # yaml-quoted if the label has a ':' or '#' in it
            return yaml.safe_dump({"label": text}, default_flow_style=False).strip()
        radios = ["# Multi-station mode - see docs/stations.md. One folder per radio/antenna system.",
                  f"default: {f}                      # which station the GUI opens on", "stations:",
                  f"  {f}:", f"    dir: {f}", f"    {label_line(a.first_label or f)}",
                  f"  {s}:", f"    dir: {s}", f"    {label_line(a.second_label or s)}", ""]
        with open(p("radios.yaml"), "w") as fh:
            fh.write("\n".join(radios))
        undo.append(lambda: os.remove(p("radios.yaml")))
        import station                       # the file just written has to be one station.py accepts
        station.RADIOS_PATH = p("radios.yaml")
        try:
            station.load_radios()
        except station.StationError as e:
            raise MigrationError(f"the generated radios.yaml isn't valid: {e}")
    except Exception as e:
        print(f"\nFAILED: {e}\nUndoing the steps already done ...", file=sys.stderr)
        for back in reversed(undo):
            try:
                back()
            except Exception as ue:
                print(f"  (couldn't undo one step: {ue})", file=sys.stderr)
        print(f"Undone. Your files are as they were; a copy is in {BACKUP_DIR}/ regardless.",
              file=sys.stderr)
        raise MigrationError(str(e)) from e


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--first", default="r2", help="name for your existing fleet's station (default: r2)")
    ap.add_argument("--second", default="mini", help="name for the new, empty station (default: mini)")
    ap.add_argument("--first-label", help="shown in the GUI, e.g. 'R2 + beam (Az/El)'")
    ap.add_argument("--second-label", help="shown in the GUI, e.g. 'Mini + helix (fixed)'")
    ap.add_argument("--second-rig-port", type=int, required=True,
                    help="the new station's Doppler (rigctld) port - must differ from the first's")
    ap.add_argument("--second-template",
                    help="a flowgraph you already run on the second radio, to install as its record-only template")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args()

    plan, problems = build_plan(a)
    if problems:
        print("Can't migrate yet:\n" + "\n".join(f"  - {x}" for x in problems), file=sys.stderr)
        return 1
    print(describe(plan, a))
    if a.dry_run:
        print("\n(--dry-run: nothing changed)")
        return 0
    if not a.yes:
        try:
            ans = input("\nProceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("Not changed.")
            return 0
    try:
        execute(plan, a)
    except MigrationError:
        return 1

    f, s = plan["first"], plan["second"]
    import station
    station.RADIOS_PATH = p("radios.yaml")
    problems = station.cross_station_conflicts()
    print(f"\nDone. '{f}' holds your fleet; '{s}' is ready to fill.")
    print("Check each station:")
    print(f"  python3 preflight.py --radio {f}")
    print(f"  python3 preflight.py --radio {s}     # expect a warning that it has no satellites yet")
    if problems:
        print("\nWARNING - the stations clash:\n" + "\n".join(f"  - {x}" for x in problems))
    if not plan.get("template_path"):
        print(f"\n{s} has no record-only template yet. Put a flowgraph you already run on that radio at\n"
              f"  {s}/flowgraphs/_record_only_template.grc\n"
              f"(or re-run with --second-template). Not generated from {f}'s on purpose: the radios differ\n"
              f"in more than a device string - e.g. the sample rates they offer (as far as I know the\n"
              f"Airspy Mini offers 3 and 6 MS/s where the R2 offers 2.5 and 10) - so a swapped copy would\n"
              f"look right and not work.")
    if plan["use_git"]:
        print("\nWhen you're happy, commit. git mv already staged the flowgraph renames, so normally all that's\n"
              "left to add is the new station folder:")
        print(f"  git add {s}")
        print("  git status          # review: renames staged; your own uncommitted edits still unstaged")
        print("  git commit -m 'move to multi-station layout'")
        print(f"(Not 'git add -A {f}': that would also commit any uncommitted flowgraph edits and untracked\n"
              f"flowgraphs sitting in {f}/flowgraphs - fine if you want them in, but a choice, not a default.)")
    else:
        print("\nWhen you're happy, commit:")
        print(f"  git add -A {f} {s}")
        print("  git status          # review")
        print("  git commit -m 'move to multi-station layout'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
