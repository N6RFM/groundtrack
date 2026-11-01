# Backup and restore

GitHub holds the program. It does **not** hold what makes your station yours: `satellites.yaml`, the `.grc` flowgraphs you built,
`sdr.local.yaml`, `radios.yaml`, `computer.local.yaml`, `schedule.yaml`, `pass_log.jsonl` and the `tle/` folder. They exist on
one disk only. `backup_state.py` saves them into dated zip files so a disk failure costs you minutes, not days.

## Set it up (once per computer)

In the GUI: **Share and back up -> Backup settings...**

| Setting | Meaning |
|---|---|
| Automatic backup | Off, Weekly (Sundays about 03:30, the default), Daily or Monthly. A run missed while the computer was off happens at next start |
| Keep the newest | How many backups to keep (default 5); older ones this tool made are deleted |
| Save backups in | Default `~/groundtrack-backups` |
| Also copy each one to | Optional `user@server:folder/` on a machine you own; each new backup is copied there after it is made |

**Save** applies the settings (and switches the schedule on or off), **Back up now** makes one immediately, **Restore...** shows what the
newest backup would change and asks before restoring, **Open folder** shows the files.

From a terminal the same thing is: `python3 backup_state.py --schedule` (or `--schedule daily`), `python3 backup_state.py --check`,
`python3 backup_state.py --keep 3 --remember`. See [Scripts reference](scripts-reference.md#backup_statepy---back-up-what-github-does-not-have).

A backup identical to the newest one is skipped, so nothing piles up while nothing changes. Backups are never overwritten: each has its own date and time in
its name.

## Privacy

A backup contains your station coordinates (they are in `satellites.yaml`). It is readable by you only, `*.zip` is ignored by git, and it must never be
uploaded to GitHub. Copy it only to machines you own.

## After a disk failure

1. Reinstall the system and the radio software, then `git clone https://github.com/N6RFM/groundtrack` and enter the folder.
2. Put a backup file into `~/groundtrack-backups` (from your other machine, or the optional copy).
3. `python3 backup_state.py --restore` shows what would change and changes nothing. Then `python3 backup_state.py --restore --yes`
   does it; any file it replaces is kept as `<name>.bak-<time>`.
4. `./regen_all.sh` rebuilds the compiled flowgraphs, `python3 preflight.py` checks every station.
5. On a **different** computer from the one the backup came from, `sdr.local.yaml` and `computer.local.yaml` are not restored (they describe the other
   computer's radios and stations). Run `python3 update_computer.py` to set those for this computer.

## What is not backed up

The program files (GitHub has them), compiled flowgraphs (rebuilt from the `.grc`), and IQ recordings and KISS files (large; back those up your own way).
