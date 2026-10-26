#!/usr/bin/env python3
"""
Keep this computer's dashboard reachable through your own server (e.g. your Oracle VM), always on.

Run from the groundtrack folder, once per computer, with a DIFFERENT --remote-port on each:

    python3 setup_dashboard_tunnel.py --server ubuntu@ORACLE_IP --remote-port 18750      # Rhode Island
    python3 setup_dashboard_tunnel.py --server ubuntu@ORACLE_IP --remote-port 18751      # Texas

It (1) makes an SSH key just for this and copies it to the server (asks for the server password
once, if needed), (2) installs two systemd USER services - the dashboard, and an SSH reverse
tunnel that restarts itself - and starts them. Both come back after a reboot.

The dashboard stays bound to 127.0.0.1 here and on the server: do NOT open the remote port in the
Oracle firewall. You view it with an SSH port-forward (printed at the end).

    --dry-run     show what would be written, change nothing
    --uninstall   stop and remove the two services
    --check       test everything on this computer and say what to fix (needs no other arguments)

If something stops working, start with:   python3 setup_dashboard_tunnel.py --check
More help: docs/remote-dashboard.md
"""
import argparse
import getpass
import os
import shlex
import re
import shutil
import subprocess
import sys
import urllib.request

HERE = os.getcwd()
KEY = os.path.expanduser("~/.ssh/id_ed25519_groundtrack")
UNITS = os.path.expanduser("~/.config/systemd/user")
DASH_PORT = 8750


def run(cmd, check=False, **kw):
    print("  $ " + " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, check=check, **kw)


def units(args):
    py = shutil.which("python3") or "/usr/bin/python3"
    ssh = shutil.which("ssh") or "/usr/bin/ssh"
    dash = f"""[Unit]
Description=groundtrack dashboard (local only)

[Service]
WorkingDirectory={HERE}
ExecStart={py} {os.path.join(HERE, 'dashboard.py')} --port {DASH_PORT}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""
    tunnel = f"""[Unit]
Description=groundtrack dashboard tunnel to {args.server}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={ssh} -N -i {KEY} -p {args.ssh_port} -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new -o BatchMode=yes -R 127.0.0.1:{args.remote_port}:127.0.0.1:{DASH_PORT} {args.server}
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""
    return {"groundtrack-dashboard.service": dash, "groundtrack-tunnel.service": tunnel}


def check():
    """Test each link of the chain and print the fix for the first broken one. Returns the number of problems."""
    bad = 0

    def line(ok, text, fix=None):
        nonlocal bad
        print(f"  [{'ok' if ok else 'FAIL'}] {text}")
        if not ok:
            bad += 1
            if fix:
                print(f"         fix: {fix}")

    def sc(*a):
        return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True).stdout.strip()

    print(f"Dashboard tunnel check - folder {HERE}\n")
    for n, what in (("groundtrack-dashboard.service", "dashboard service"), ("groundtrack-tunnel.service", "tunnel service")):
        st = sc("is-active", n)
        line(st == "active", f"{what} is {st or 'not installed'}",
             f"systemctl --user restart {n}   (not installed? run this script with --server and --remote-port)")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DASH_PORT}/api/status", timeout=10) as r:
            line(r.status == 200, f"dashboard answers on this computer (port {DASH_PORT})")
    except Exception as e:
        line(False, f"dashboard does not answer on port {DASH_PORT} ({e})",
             f"stop any hand-started dashboard.py, then: systemctl --user restart groundtrack-dashboard.service ; "
             f"details: journalctl --user -u groundtrack-dashboard.service -n 20")
    unit = os.path.join(UNITS, "groundtrack-tunnel.service")
    m = re.search(r"-R 127\.0\.0\.1:(\d+):127\.0\.0\.1:\d+ (\S+)", open(unit).read()) if os.path.exists(unit) else None
    if not m:
        line(False, "tunnel service file not found", "run this script with --server user@host --remote-port N")
    else:
        port, server = m.group(1), m.group(2)
        r = subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", server,
                            f"curl -s -m 10 -o /dev/null -w %{{http_code}} 127.0.0.1:{port}/api/status"],
                           capture_output=True, text=True)
        if r.returncode == 255:
            line(False, f"cannot log in to {server} with this computer's key",
                 "the server may be down or the key was removed from ~/.ssh/authorized_keys on it; "
                 "see docs/remote-dashboard.md ('key refused'). Check the VM is running in the Oracle console")
        else:
            line(r.stdout.strip() == "200", f"the server sees this computer's dashboard on port {port}",
                 "systemctl --user restart groundtrack-tunnel.service ; wait 20 s ; run --check again. "
                 "Still failing: journalctl --user -u groundtrack-tunnel.service -n 20")
    print("\nAll good." if not bad else f"\n{bad} problem(s).")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="user@host of your server, e.g. ubuntu@203.0.113.5")
    ap.add_argument("--remote-port", type=int, help="port on the server for this computer (use a different one per computer)")
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(os.path.join(HERE, "dashboard.py")):
        sys.exit("Run this from the groundtrack folder (dashboard.py not found here). Run update_computer.py first.")

    if args.check:
        sys.exit(1 if check() else 0)

    if args.uninstall:
        for n in ("groundtrack-tunnel.service", "groundtrack-dashboard.service"):
            run(["systemctl", "--user", "disable", "--now", n])
            p = os.path.join(UNITS, n)
            if os.path.exists(p):
                os.remove(p)
        run(["systemctl", "--user", "daemon-reload"])
        print("Removed.")
        return

    if not args.server or not args.remote_port:
        sys.exit("need --server user@host and --remote-port N  (see --help)")
    u = units(args)
    if args.dry_run:
        for n, t in u.items():
            print(f"--- {n}\n{t}")
        print("dry run: nothing changed.")
        return
    if not shutil.which("ssh") or not shutil.which("systemctl"):
        sys.exit("needs ssh and systemd (systemctl) on this computer")

    print("1. SSH key for the tunnel")
    if not os.path.exists(KEY):
        os.makedirs(os.path.dirname(KEY), mode=0o700, exist_ok=True)
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"groundtrack-{getpass.getuser()}", "-f", KEY], check=True)
    test = ["ssh", "-i", KEY, "-p", str(args.ssh_port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15", args.server, "true"]
    if run(test, stderr=subprocess.DEVNULL).returncode != 0:
        print("   copying the key to the server (enter the server password if asked)...")
        r = run(["ssh-copy-id", "-i", KEY + ".pub", "-p", str(args.ssh_port), "-o", "StrictHostKeyChecking=accept-new", args.server])
        if r.returncode != 0 or run(test, stderr=subprocess.DEVNULL).returncode != 0:
            print("\nCould not log in with the key. Add this line to ~/.ssh/authorized_keys on the server, then re-run:\n")
            print(open(KEY + ".pub").read())
            sys.exit(1)
    print("   key works.")

    print("2. Services")
    os.makedirs(UNITS, exist_ok=True)
    for n, t in u.items():
        with open(os.path.join(UNITS, n), "w") as f:
            f.write(t)
    run(["systemctl", "--user", "daemon-reload"], check=True)
    run(["systemctl", "--user", "enable", "--now", "groundtrack-dashboard.service", "groundtrack-tunnel.service"], check=True)
    if run(["loginctl", "enable-linger", getpass.getuser()]).returncode != 0:
        print("   (could not enable start-without-login; run: sudo loginctl enable-linger " + getpass.getuser() + ")")

    host = args.server.split("@")[-1]
    print(f"""
Done. If a dashboard.py is already running by hand in a terminal, close it (the service uses port {DASH_PORT}).
This computer's dashboard is now on the server at 127.0.0.1:{args.remote_port}.

To view it from anywhere, run on your laptop/phone-terminal:
    ssh -L 8760:127.0.0.1:{args.remote_port} {args.server}
then open http://127.0.0.1:8760
(For both computers: add  -L 8761:127.0.0.1:<the other computer's remote port>  and open 8761.)
Check status here:  systemctl --user status groundtrack-tunnel.service   (server: {host})""")


if __name__ == "__main__":
    main()
