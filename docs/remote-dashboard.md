# Viewing a dashboard from another computer

`dashboard.py` shows one computer's stations as a web page (see [Scripts reference](scripts-reference.md#dashboardpy---the-same-report-as-a-live-web-page)).
To see the OTHER computer's dashboard, each computer keeps a private tunnel open to your own server (the Oracle VM).
Nothing goes through GitHub and nothing is open to the internet: the dashboard has no login, so the tunnel ports
on the VM must stay closed in the Oracle firewall.

```
 Rhode Island :8750 --tunnel--> VM 127.0.0.1:18750 --\
                                                       >-- your laptop: ssh -L ... gtview --> browser
 Texas        :8750 --tunnel--> VM 127.0.0.1:18751 --/
```

## Everyday use

On any computer that has the key (RI and Texas both do), keep this running in a terminal:
```
gtview
```
(an alias for `ssh -4 -i ~/.ssh/id_ed25519_groundtrack -L 8760:127.0.0.1:18750 -L 8761:127.0.0.1:18751 ubuntu@REDACTED`)

Then open `http://127.0.0.1:8760` (Rhode Island) and `http://127.0.0.1:8761` (Texas). This computer's own dashboard
is always at `http://127.0.0.1:8750`. If the page says "dashboard server not reachable", the path behind it is down.

**Which computer is this?** The page title and heading say `groundtrack - <site>`, and each station shows its 6-character
grid square (for example `FN41iq`, about 5 x 2.5 km - no exact coordinates are shown). The site name is this computer's host
name unless you set one (once per computer, in the groundtrack folder):
```
echo "site: Rhode Island" >> computer.local.yaml      # Texas: echo "site: Texas" >> computer.local.yaml
systemctl --user restart groundtrack-dashboard.service
```

## When something breaks: run the check first

```
cd ~/groundtrack            # or ~/Desktop/groundtrack
python3 setup_dashboard_tunnel.py --check
```
It tests each link and prints the fix for any that fail:

| It says | Meaning / fix |
|---|---|
| dashboard service is not active | `systemctl --user restart groundtrack-dashboard.service` |
| dashboard does not answer on port 8750 | A hand-started `dashboard.py` may hold the port: `pkill -f dashboard.py`, then restart the service. Details: `journalctl --user -u groundtrack-dashboard.service -n 20` |
| tunnel service is not active | `systemctl --user restart groundtrack-tunnel.service` |
| cannot log in to the server with this computer's key | Server down (check the VM in the Oracle console), or the key was removed - see "key refused" below |
| the server does not see this computer's dashboard | Restart the tunnel service, wait 20 s, check again |

## Problems in the viewing window (`gtview`)

- **`bind [::1]:... Cannot assign requested address`** - harmless (this computer has no IPv6 loopback). `gtview` uses `ssh -4` to avoid it.
- **`bind ... Address already in use`** - an earlier `gtview` is still open. Either use it, or close it:
  `ss -ltnp | grep -E ':876[01]'` shows its PID; `kill PID`; run `gtview` again.
- **`gtview: command not found`** - define it (once per computer):
  `echo "alias gtview='ssh -4 -i ~/.ssh/id_ed25519_groundtrack -L 8760:127.0.0.1:18750 -L 8761:127.0.0.1:18751 ubuntu@REDACTED'" >> ~/.bashrc; source ~/.bashrc`
- **Page loads but is blank/old** - the computer behind it is off or its tunnel is down; run `--check` ON that computer.
- **The ssh window closed** - the forwards ended; run `gtview` again.

## Key refused ("Permission denied (publickey)")

Each computer has its own key `~/.ssh/id_ed25519_groundtrack`; the VM must list its public key in
`~/.ssh/authorized_keys` (user `ubuntu`). To add or re-add one, run this ON A COMPUTER WHOSE KEY STILL WORKS,
with the other computer's line from `cat ~/.ssh/id_ed25519_groundtrack.pub`:
```
ssh -i ~/.ssh/id_ed25519_groundtrack ubuntu@REDACTED 'echo "PASTE THE WHOLE ssh-ed25519 LINE HERE" >> ~/.ssh/authorized_keys'
```
If NO computer can log in any more, get a terminal on the VM another way (Oracle console / Cloud Shell console
connection, or RustDesk to the VM) and run the `echo ... >> ~/.ssh/authorized_keys` there.

## After the VM is replaced or its address changes

Re-run the setup on each computer with the new address (it rewrites the tunnel service):
```
python3 setup_dashboard_tunnel.py --server ubuntu@NEW_ADDRESS --remote-port 18750   # Rhode Island
python3 setup_dashboard_tunnel.py --server ubuntu@NEW_ADDRESS --remote-port 18751   # Texas
```
and update the address in the `gtview` alias (`nano ~/.bashrc`).

## Setting up a new computer

```
cd ~/groundtrack && git pull
python3 setup_dashboard_tunnel.py --server ubuntu@REDACTED --remote-port 18752    # a NEW number per computer
```
It stops at "Could not log in with the key" and prints a line: add it to the VM as shown under "Key refused".
Run it again; it should say "key works". Add the new port to `gtview` with another `-L 8762:127.0.0.1:18752`.

## Turning it off

`python3 setup_dashboard_tunnel.py --uninstall` removes both services on that computer.
