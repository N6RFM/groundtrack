#!/usr/bin/env python3
"""
groundtrack_gui.py - a simple GUI over the existing
groundtrack scripts.

Deliberately does NOT modify, import, or depend on the internals of any
existing script. It only does two things:
  1. Reads satellites.yaml directly (read-only) and does cheap, obvious
     gap checks itself (does the .grc exist? the .py? is the .py older
     than the .grc?) for instant table feedback.
  2. Shells out to the real, unmodified scripts for everything else -
     preflight.py for the full/authoritative check, toggle_satellite.py
     to enable/disable, add_satellite.py to add, update_tle.py to
     refresh. Whatever each one prints goes straight into the output
     pane, unparsed and unmodified.

If this prototype is abandoned, nothing about the real toolkit needs to
be undone - this file can just be deleted.

Run from the repo root, same as every other script here:
    python3 groundtrack_gui.py
"""

import contextlib
import io
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

import yaml

import station
from doctor import check_stray_compiled_files

CONFIG_PATH = "satellites.yaml"


def load_satellites():
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("satellites", [])


def slug_for(sat):
    # mirrors add_satellite.py's own slugify closely enough for display
    # purposes - this is a rough read-only guess, not authoritative
    return sat.get("script", "").replace("flowgraphs/", "").replace(".py", "")


def suggest_next_ports(sats):
    """Read-only preview of what add_satellite.py would auto-assign -
    mirrors its own next_free_ports() algorithm exactly, purely to
    pre-fill sensible defaults in the Add satellite form. The GUI never
    writes ports itself; add_satellite.py re-derives and validates them
    independently when actually called."""
    used_producer = {s["producer_port"] for s in sats if "producer_port" in s}
    used_consumer = {s["consumer_port"] for s in sats if "consumer_port" in s}
    elsewhere = station.other_stations_ports()   # empty unless multi-station
    used_producer |= elsewhere
    used_consumer |= elsewhere
    prod = 9101
    while prod in used_producer:
        prod += 1
    cons = 8101
    while cons in used_consumer:
        cons += 1
    return prod, cons


def gap_check(sat):
    """Cheap, obvious checks done directly - not a replacement for
    preflight.py, just instant table feedback before running it."""
    slug = slug_for(sat)
    grc_path = f"flowgraphs/{slug}.grc"
    py_path = f"flowgraphs/{slug}.py"

    problems = []
    if not sat.get("script"):
        problems.append("no script: field")
    if not os.path.exists(grc_path):
        problems.append(f"missing {grc_path}")
    elif not os.path.exists(py_path):
        problems.append(f"missing {py_path} (never grcc'd)")
    elif os.path.getmtime(py_path) < os.path.getmtime(grc_path):
        problems.append(f"{py_path} older than {grc_path} (stale, needs grcc)")

    uses_relay = "producer_port" in sat or "consumer_port" in sat
    if uses_relay and ("producer_port" not in sat or "consumer_port" not in sat):
        problems.append("has only one of producer_port/consumer_port")

    return problems


def find_terminal():
    """Return the first available terminal emulator's launch command
    prefix, or None if none of the common ones are installed."""
    import shutil
    candidates = [
        ("gnome-terminal", ["gnome-terminal", "--"]),
        ("konsole", ["konsole", "-e"]),
        ("xfce4-terminal", ["xfce4-terminal", "-e"]),
        ("x-terminal-emulator", ["x-terminal-emulator", "-e"]),
        ("xterm", ["xterm", "-e"]),
    ]
    for binary, prefix in candidates:
        if shutil.which(binary):
            return prefix
    return None


def _show(args):
    """A command as it should read in the output pane. Scripts are launched
    by absolute path now - a bare 'preflight.py' wouldn't be found from
    inside a station folder - but the full path on every line would bury the
    command, so the project directory is trimmed back off for display."""
    return " ".join(args).replace(station.SCRIPT_DIR + os.sep, "")


class GroundtrackGUI(tk.Tk):
    def __init__(self):
        # Before any window exists, so a bad --radio fails cleanly instead of
        # leaving a half-built window behind.
        initial = self._startup_station()
        super().__init__()
        self.title("groundtrack - fleet overview")
        self.geometry("900x600")

        # None = classic single-folder mode: no station bar, nothing changes.
        self.stations = station.load_radios()[1] if initial else None
        self.current_station = initial

        self._build_station_bar()      # packed first, so it stays pinned above the scrolling area
        self._build_scroll_container()
        self.repo_root = os.getcwd()
        self._build_table()
        self._build_actions()
        self._build_output()
        if initial:
            self._station_changed()
            self._poll_stations()
        else:
            self.refresh()

    # ---- stations (only when radios.yaml exists) ---------------------------

    def _startup_station(self):
        """Classic mode (no radios.yaml): returns None, leaving everything
        exactly as it was. Otherwise works out which station to open on and
        changes into it. Never prompts - --radio, then GROUNDTRACK_STATION,
        then radios.yaml's own default (R2, the beam system) - and never
        remembers the last session's choice: being surprised about which radio
        you're pointed at is the wrong kind of surprise."""
        if not station.multi_station():
            if station.pop_radio_arg():
                raise station.StationError("--radio was given, but there's no radios.yaml")
            return None
        name = station.initial_station()
        station.switch(name)
        return name

    def _build_station_bar(self):
        self.station_var = tk.StringVar()
        self.station_buttons = {}
        if not self.stations:
            return
        # Which radio you're pointed at has physical consequences - run_passes
        # on the beam station moves an antenna - so the active one has to be
        # unmistakable at a glance, not a subtle thin border: bold, a strong
        # colour, sunken. (Style named Station.Toolbutton to inherit the
        # Toolbutton layout, which is what makes a Radiobutton look like a
        # push-in switch rather than a dot and a label.)
        from tkinter import font as tkfont
        self._station_font = tkfont.nametofont("TkDefaultFont").copy()
        self._station_font.configure(weight="bold", size=self._station_font.cget("size") + 1)
        style = ttk.Style(self)
        style.configure("Station.Toolbutton", padding=(16, 6), font=self._station_font)
        style.map("Station.Toolbutton",
                  background=[("selected", "#8fd18f"), ("active", "#e6e6e6")],
                  relief=[("selected", "sunken"), ("!selected", "raised")])

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="Active station:").pack(side="left")
        for name in self.stations:
            btn = ttk.Radiobutton(bar, text=name, value=name, variable=self.station_var,
                                  style="Station.Toolbutton",
                                  command=lambda n=name: self.switch_station(n))
            btn.pack(side="left", padx=(8, 0))
            self.station_buttons[name] = btn
        # the one thing that most needs to be visible: does this station move an antenna?
        self.rotor_badge = tk.Label(bar, text="", padx=10, pady=3, font=self._station_font)
        self.rotor_badge.pack(side="left", padx=(14, 0))
        self.station_info = ttk.Label(bar, text="")
        self.station_info.pack(side="left", padx=12)
        ttk.Separator(self, orient="horizontal").pack(side="top", fill="x")

    def switch_station(self, name):
        if name == self.current_station:
            return
        pid = station.run_passes_pid(name)
        if pid:
            messagebox.showinfo(
                "Already running",
                f"run_passes.py is already running for station '{name}' (PID {pid}).\n\n"
                f"Switching here only changes what this window shows and controls - "
                f"that run carries on, untouched, in its own terminal. Starting a "
                f"second one for '{name}' would be refused automatically.")
        try:
            station.switch(name)
        except station.StationError as e:
            messagebox.showerror("Can't switch station", str(e))
            self.station_var.set(self.current_station)  # put the toggle back
            return
        self.current_station = name
        self.repo_root = os.getcwd()
        # leftover output from the other radio, sitting there after a switch,
        # is exactly the kind of thing that gets misread as current
        self.clear_output()
        self._station_changed()

    def _station_changed(self):
        self.title(f"groundtrack - fleet overview [{self.current_station}]")
        self.station_var.set(self.current_station)
        self.refresh()

    def _update_station_bar(self):
        """Running markers on every station's button, and the active station's
        details (rotor or none, rig port, whether run_passes.py is live)."""
        if not self.stations:
            return
        try:
            for name, btn in self.station_buttons.items():
                btn.config(text=f"{name}  \u25cf" if station.run_passes_pid(name) else name)
            d = station.describe(self.current_station)
        except station.StationError:
            return
        if d["rotor"]:
            self.rotor_badge.config(text=f"BEAM CONTROL ON  ({d['rotor']})",
                                    background="#f4a259", foreground="#000000")
        else:
            self.rotor_badge.config(text="no rotor - antenna never moved",
                                    background="#dcdcdc", foreground="#333333")
        rig = f"rig port {d['rig_port']}" if d["rig_port"] else "rig port not set"
        text = f"{d['label']}   -   {rig}"
        if d["running_pid"]:
            text += f"   -   run_passes.py RUNNING (PID {d['running_pid']})"
        self.station_info.config(text=text,
                                 foreground="#b35900" if d["running_pid"] else "#444444")

    def _poll_stations(self):
        """run_passes.py starts and stops in terminals this window doesn't
        control, so the markers are re-read every few seconds."""
        self._update_station_bar()
        self.after(3000, self._poll_stations)

    def _build_scroll_container(self):
        """Wraps everything below in a scrollable area with a slider on
        the right edge of the window, so the full layout (table, five
        button rows, output pane) stays reachable even if the window
        ends up shorter than its content on a smaller screen."""
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # this is what every _build_* method below actually adds widgets to
        self.content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=self.content, anchor="nw")

        def on_content_resize(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        self.content.bind("<Configure>", on_content_resize)

        def on_canvas_resize(event):
            # stretch the inner frame to the canvas's full width, so
            # LabelFrames/buttons don't just hug the left edge
            canvas.itemconfig(content_window, width=event.width)
        canvas.bind("<Configure>", on_canvas_resize)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", on_mousewheel)          # Windows/macOS
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))  # Linux
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))   # Linux

    def _build_table(self):
        table_frame = ttk.Frame(self.content)
        table_frame.pack(fill="x", padx=8, pady=8)

        columns = ("name", "norad", "freq", "enabled", "mode", "gaps")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=10)
        headings = {
            "name": "Satellite", "norad": "NORAD", "freq": "Freq (Hz)",
            "enabled": "Enabled", "mode": "Mode", "gaps": "Gaps found",
        }
        widths = {"name": 110, "norad": 70, "freq": 100, "enabled": 70,
                  "mode": 110, "gaps": 350}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.tag_configure("hasgaps", background="#ffe4e4")
        self.tree.tag_configure("disabled", foreground="#888888")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

    def _build_actions(self):
        row1 = ttk.LabelFrame(self.content, text="Satellite management")
        row1.pack(fill="x", padx=8, pady=(4, 4))

        row1a = ttk.Frame(row1)
        row1a.pack(fill="x", pady=(2, 2))
        ttk.Button(row1a, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(row1a, text="Add satellite...",
                   command=self.add_satellite_dialog).pack(side="left", padx=4)
        ttk.Button(row1a, text="Edit selected",
                   command=self.edit_satellite_dialog).pack(side="left")
        ttk.Button(row1a, text="View YAML for selected",
                   command=self.view_yaml_selected).pack(side="left", padx=4)
        ttk.Button(row1a, text="Enable selected",
                   command=lambda: self.toggle(True)).pack(side="left", padx=4)
        ttk.Button(row1a, text="Disable selected",
                   command=lambda: self.toggle(False)).pack(side="left")

        row1b = ttk.Frame(row1)
        row1b.pack(fill="x", pady=(2, 2))
        ttk.Button(row1b, text="Regenerate .grc for selected",
                   command=self.regen_selected).pack(side="left")
        ttk.Button(row1b, text="Vet selected .grc",
                   command=self.vet_selected).pack(side="left", padx=4)
        ttk.Button(row1b, text="Vet --fix selected .grc",
                   command=self.vet_fix_selected).pack(side="left")
        ttk.Button(row1b, text="Suggest extra_outputs (new window)",
                   command=self.suggest_extra_outputs_selected).pack(side="left", padx=4)
        ttk.Separator(row1b, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(row1b, text="Delete selected",
                   command=self.delete_selected).pack(side="left")

        row2 = ttk.LabelFrame(self.content, text="Checks and maintenance")
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Button(row2, text="Run preflight.py (full check)",
                   command=self.run_preflight).pack(side="left")
        ttk.Button(row2, text="Run doctor.py",
                   command=self.run_doctor).pack(side="left", padx=4)
        ttk.Button(row2, text="Run doctor.py --fix",
                   command=self.run_doctor_fix).pack(side="left")

        row3 = ttk.LabelFrame(self.content, text="TLE data")
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Button(row3, text="Run update_tle.py",
                   command=self.run_update_tle).pack(side="left")

        row4 = ttk.LabelFrame(self.content, text="Pass scheduler")
        row4.pack(fill="x", padx=8, pady=4)
        ttk.Button(row4, text="Show schedule",
                   command=self.show_schedule).pack(side="left")
        ttk.Button(row4, text="Plan passes (auto-approve)",
                   command=self.plan_passes_auto).pack(side="left", padx=4)
        ttk.Button(row4, text="Plan passes (interactive, new window)",
                   command=self.plan_passes_interactive).pack(side="left")
        ttk.Button(row4, text="Toggle IQ for pass (new window)",
                   command=self.toggle_pass_record_iq).pack(side="left", padx=4)
        ttk.Button(row4, text="Show pass log",
                   command=self.show_pass_log).pack(side="left")

        row5 = ttk.LabelFrame(self.content, text="Relay / Bridge")
        row5.pack(fill="x", padx=8, pady=4)
        ttk.Button(row5, text="Start relay.py (new window, verbose)",
                   command=self.start_relay).pack(side="left")
        ttk.Button(row5, text="Start tcp_bridge.py (new window, verbose)",
                   command=self.start_tcp_bridge).pack(side="left", padx=4)

        row6 = ttk.LabelFrame(self.content, text="Execution")
        row6.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Button(row6, text="Start run_passes.py (new window)",
                   command=self.start_run_passes).pack(side="left")
        self.preposition_var = tk.BooleanVar(value=True)
        self.preposition_check = ttk.Checkbutton(row6, text=self.PREPOSITION_TEXT,
                                                  variable=self.preposition_var)
        self.preposition_check.pack(side="left", padx=(8, 0))
        self.record_iq_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row6, text="Record IQ",
                         variable=self.record_iq_var).pack(side="left", padx=(8, 0))

    def _build_output(self):
        header = ttk.Frame(self.content)
        header.pack(fill="x", padx=8)
        ttk.Label(header, text="Output from the last command run:").pack(side="left")
        ttk.Button(header, text="Clear", command=self.clear_output).pack(side="right")
        ttk.Button(header, text="Copy", command=self.copy_output).pack(
            side="right", padx=8)
        ttk.Button(header, text="Save to file...",
                   command=self.save_output).pack(side="right", padx=4)

        output_frame = ttk.Frame(self.content)
        output_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        scrollbar = ttk.Scrollbar(output_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        self.output = tk.Text(output_frame, height=18, bg="#111", fg="#ddd",
                               font=("Courier", 10), wrap="word",
                               yscrollcommand=scrollbar.set)
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.output.yview)

    def clear_output(self):
        self.output.delete("1.0", tk.END)

    def copy_output(self):
        text = self.output.get("1.0", tk.END)
        self.clipboard_clear()
        self.clipboard_append(text)

    def save_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="groundtrack_output.txt")
        if not path:
            return
        with open(path, "w") as f:
            f.write(self.output.get("1.0", tk.END))

    def log(self, text):
        self.output.delete("1.0", tk.END)
        self.output.insert(tk.END, text)

    def run_cmd(self, args):
        """Run an existing script exactly as you'd type it - never
        touches its source, just captures what it prints.

        Inserts -u (unbuffered) right after the interpreter when the
        command is a python invocation. Without it, a script's stdout
        switches from line-buffered to fully block-buffered the moment
        it's a pipe rather than a real terminal (exactly what
        capture_output=True creates) - if that script itself launches a
        nested subprocess (like doctor.py calling preflight.py), the
        outer script's own buffered output can sit unflushed while the
        inner one runs, producing scrambled ordering or, in doctor.py's
        case, output that never completes at all. -u forces immediate
        flushing regardless of whether stdout is a terminal or a pipe.

        Returns (returncode, output) - callers that need to know whether
        the command actually succeeded (like the Add satellite form,
        which needs to decide whether to close itself or stay open for
        a retry) can check returncode rather than guessing from text."""
        if args and args[0] == sys.executable and "-u" not in args:
            args = [args[0], "-u"] + args[1:]
        self.log(f"$ {_show(args)}\n\n(running...)\n")
        self.update_idletasks()
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                     timeout=120)
            output = result.stdout + result.stderr
            returncode = result.returncode
        except Exception as e:
            output = f"Failed to run: {e}"
            returncode = -1
        self.log(f"$ {_show(args)}\n\n{output}")
        return returncode, output

    PREPOSITION_TEXT = "Pre-position rotor for next pass"

    def _has_rotor(self):
        """Does the active station steer an antenna? The test run_passes.py itself uses: both
        rot_host and rot_port in its config (the current folder's, once a station is active).
        If the config can't be read there's nothing to go on, so say yes - the box then stays
        as it always was."""
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return True
        return "rot_host" in cfg and "rot_port" in cfg

    def _update_rotor_controls(self):
        """Pre-positioning moves a rotor, so on a station without one the box is meaningless:
        grey it out and say why. Its ticked/unticked state is left alone, so the choice made
        for the beam station is still there when you switch back to it."""
        if not hasattr(self, "preposition_check"):
            return
        if self._has_rotor():
            self.preposition_check.configure(state="normal", text=self.PREPOSITION_TEXT)
        else:
            self.preposition_check.configure(
                state="disabled", text=f"{self.PREPOSITION_TEXT} (no rotor on this station)")

    def refresh(self):
        self._update_station_bar()
        self._update_rotor_controls()
        self.tree.delete(*self.tree.get_children())
        sats = load_satellites()
        if not sats and not os.path.exists(CONFIG_PATH):
            if self.current_station:
                self.log(f"No {CONFIG_PATH} found for station '{self.current_station}' "
                          f"(looked in {os.getcwd()}).\n"
                          f"Create one there - copy satellites.example.yaml and edit it.")
            else:
                self.log(f"No {CONFIG_PATH} found in the current directory.\n"
                          f"Run this GUI from the repo root, same as every other script.")
            return
        for sat in sats:
            name = sat.get("name", "?")
            enabled = sat.get("enabled", True)
            uses_relay = "producer_port" in sat or "consumer_port" in sat
            mode = "decode+relay" if uses_relay else "recording-only"
            problems = gap_check(sat)
            gaps_text = "; ".join(problems) if problems else "none"

            tags = []
            if problems:
                tags.append("hasgaps")
            if not enabled:
                tags.append("disabled")

            self.tree.insert("", "end", iid=name, values=(
                name, sat.get("norad", "?"), sat.get("freq_hz", "?"),
                "yes" if enabled else "no", mode, gaps_text,
            ), tags=tags)

    def selected_name(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a satellite in the table first.")
            return None
        return sel[0]

    def toggle(self, enable):
        name = self.selected_name()
        if not name:
            return
        flag = "--enable" if enable else "--disable"
        self.run_cmd([sys.executable, station.script_path("toggle_satellite.py"), flag, name])
        self.refresh()

    def regen_selected(self):
        name = self.selected_name()
        if not name:
            return
        sats = load_satellites()
        sat = next((s for s in sats if s["name"] == name), None)
        if not sat:
            return
        grc_path = f"flowgraphs/{slug_for(sat)}.grc"
        if not os.path.exists(grc_path):
            messagebox.showerror("Missing .grc", f"{grc_path} doesn't exist - "
                                  f"nothing to regenerate. Use add_satellite.py first.")
            return
        # -o flowgraphs, matching regen_all.sh: grcc otherwise writes its
        # .py to the current directory, not the .grc's own folder. The
        # stray-sweep below still runs every time, as a safety net: anything
        # that lands in the current directory anyway (an embedded block's
        # module, say) is moved into flowgraphs/, where the flowgraph imports
        # it from - never deleted.
        returncode, output = self.run_cmd(["grcc", "-o", os.path.dirname(grc_path), grc_path])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            check_stray_compiled_files(fix=True)
        sweep_output = buf.getvalue()
        if "none found" not in sweep_output:
            self.log(f"$ grcc -o {os.path.dirname(grc_path)} {grc_path}\n\n{output}\n\n"
                      f"--- stray-file check ---\n{sweep_output}")
        self.refresh()

    def _grc_path_for_selected(self):
        name = self.selected_name()
        if not name:
            return None, None
        sats = load_satellites()
        sat = next((s for s in sats if s["name"] == name), None)
        if not sat:
            return None, None
        grc_path = f"flowgraphs/{slug_for(sat)}.grc"
        if not os.path.exists(grc_path):
            messagebox.showerror("Missing .grc", f"{grc_path} doesn't exist.")
            return None, None
        return name, grc_path

    def view_yaml_selected(self):
        """Read-only - shows exactly what's in satellites.yaml for this one
        satellite, all fields at once (extra_outputs, record_iq_toggle,
        decoder file, everything), separate from the Edit dialog's
        field-by-field editing which only shows one thing at a time."""
        name = self.selected_name()
        if not name:
            messagebox.showinfo("No selection", "Select a satellite first.")
            return
        sats = load_satellites()
        sat = next((s for s in sats if s["name"] == name), None)
        if not sat:
            messagebox.showerror("Not found", f"{name} not found in satellites.yaml.")
            return

        win = tk.Toplevel(self)
        win.title(f"satellites.yaml - {name}")
        win.geometry("640x480")

        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=8, pady=8)
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        text = tk.Text(text_frame, wrap="none", yscrollcommand=scrollbar.set,
                        font=("monospace", 10))
        text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=text.yview)

        yaml_str = yaml.dump({name: sat}, sort_keys=False, default_flow_style=False)
        text.insert("1.0", yaml_str)
        text.config(state="disabled")  # read-only

        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_row, text="Copy to clipboard",
                   command=lambda: (win.clipboard_clear(),
                                     win.clipboard_append(yaml_str))).pack(side="left")
        ttk.Button(btn_row, text="Close", command=win.destroy).pack(side="right")

    def vet_selected(self):
        name, grc_path = self._grc_path_for_selected()
        if not grc_path:
            return
        self.run_cmd([sys.executable, station.script_path("vet_grc.py"), grc_path])

    def vet_fix_selected(self):
        name, grc_path = self._grc_path_for_selected()
        if not grc_path:
            return
        confirmed = messagebox.askyesno(
            "Vet --fix",
            f"Apply automatic fixes to {grc_path}?\n\n"
            f"Only ever renames a block or corrects options.id - never touches "
            f"wiring or removes anything. A .bak of the current file is kept, "
            f"and the exact diff will be shown in the output pane below.")
        if not confirmed:
            return
        self.run_cmd([sys.executable, station.script_path("vet_grc.py"), "--fix", grc_path])

    def suggest_extra_outputs_selected(self):
        """Interactive (prompts for a name, and for tcp_bridge candidates a
        bridge_port) - needs a real terminal, same reasoning as
        relay.py/tcp_bridge.py/run_passes.py: the captured output pane
        can't feed it stdin."""
        name, grc_path = self._grc_path_for_selected()
        if not grc_path:
            return
        self.spawn_in_terminal([sys.executable, station.script_path("suggest_extra_outputs.py"), name])

    def delete_selected(self):
        name = self.selected_name()
        if not name:
            return
        confirmed = messagebox.askyesno(
            "Delete satellite",
            f"Remove {name}'s entry from {CONFIG_PATH}?\n\n"
            f"Its .grc/.py files are NOT deleted by this - only the config "
            f"entry goes away.")
        if not confirmed:
            return
        self.run_cmd([sys.executable, station.script_path("delete_satellite.py"), name, "--yes"])
        self.refresh()

    def run_preflight(self):
        self.run_cmd([sys.executable, station.script_path("preflight.py")])

    def run_doctor(self):
        self.run_cmd([sys.executable, station.script_path("doctor.py")])

    def run_doctor_fix(self):
        self.run_cmd([sys.executable, station.script_path("doctor.py"), "--fix"])
        self.refresh()

    def spawn_in_terminal(self, cmd):
        """Launch cmd (a list, e.g. [sys.executable, 'relay.py', '--verbose'])
        detached in its own terminal window. Explicitly cd's to the repo
        root first and holds the window open after the process exits,
        whether that exit is normal or a crash - without both of these,
        a terminal emulator like gnome-terminal can silently start in the
        wrong directory (its client/server model doesn't reliably inherit
        this process's cwd) and close the instant the command inside it
        exits, hiding any real error before you can read it."""
        import shlex
        prefix = find_terminal()
        if prefix is None:
            messagebox.showwarning(
                "No terminal emulator found",
                "Couldn't find gnome-terminal, konsole, xfce4-terminal, "
                "x-terminal-emulator, or xterm on this system.\n\n"
                "Run this manually instead, in your own terminal:\n\n"
                f"  cd {self.repo_root}\n  {' '.join(cmd)}")
            return False

        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        # Exported inside the shell line rather than relying on this process's
        # environment: gnome-terminal's client/server model doesn't reliably
        # pass the launcher's environment through any more than it does its
        # working directory (the same reason for the explicit cd below).
        env_line = (f"export GROUNDTRACK_STATION={shlex.quote(self.current_station)}; "
                    if self.current_station else "")
        shell_line = (
            f"{env_line}cd {shlex.quote(self.repo_root)} && {quoted_cmd}; "
            f"echo; echo '--- process exited (see above for any error) ---'; "
            f"echo 'Press Ctrl-C or close this window to dismiss.'; "
            f"sleep infinity"
        )
        try:
            subprocess.Popen(prefix + ["bash", "-c", shell_line])
            self.log(f"Launched in a new window (in {self.repo_root}):\n"
                     f"$ {_show(cmd)}\n\n"
                     f"This GUI does not control it from here - use that "
                     f"window's own terminal to watch it, read any error, "
                     f"or Ctrl-C it. The window stays open after it exits "
                     f"so you can actually see what happened.")
            return True
        except Exception as e:
            messagebox.showerror("Failed to launch", str(e))
            return False

    def start_relay(self):
        """relay.py is a persistent process that runs indefinitely and
        never exits on its own - same reasoning as run_passes.py, this
        needs its own detached terminal, not a captured/blocking call
        that would freeze the GUI the moment it's clicked."""
        self.spawn_in_terminal([sys.executable, station.script_path("relay.py"), "--verbose"])

    def start_tcp_bridge(self):
        """Same reasoning as relay.py - a persistent process for
        satellites whose flowgraph runs its own TCP_SERVER instead of
        connecting out as a client, needing its own detached terminal."""
        self.spawn_in_terminal([sys.executable, station.script_path("tcp_bridge.py"), "--verbose"])

    def start_run_passes(self):
        """run_passes.py waits indefinitely for AOS and never exits on its
        own - running it the same way as preflight.py/doctor.py would
        freeze the whole GUI until it was killed. It needs its own
        detached process with its own visible terminal instead, so you
        can watch its live output and Ctrl-C it independently."""
        if self.current_station:
            pid = station.run_passes_pid(self.current_station)
            if pid:
                # run_passes.py would refuse on its own (same lock file) - this
                # just says so now, instead of in a terminal window that opens,
                # prints an error and sits there
                messagebox.showwarning(
                    "Already running",
                    f"run_passes.py is already running for station "
                    f"'{self.current_station}' (PID {pid}).\n\nA second one would "
                    f"refuse to start - they'd fight over rigctld/rotctld and the "
                    f"SDR. Use that run's own terminal window, or Ctrl-C it first.")
                return
        interval = simpledialog.askfloat(
            "Start run_passes.py",
            "Status line update interval (seconds) - how often the "
            "el/az/freq/Doppler line refreshes while a pass is active. "
            "Doppler correction itself still recomputes every second "
            "regardless; the rotor only gets a new command when the "
            "satellite has actually drifted enough (lead-ahead targeting, "
            "not a fixed cadence) - this setting only controls how often "
            "the line is redrawn:",
            initialvalue=5.0, minvalue=0.1)
        if interval is None:
            return
        cmd = [sys.executable, station.script_path("run_passes.py"), "--verbose",
               "--status-interval", str(interval)]
        if self._has_rotor() and not self.preposition_var.get():
            cmd.append("--no-preposition")      # nothing to pre-position on a station with no rotor
        cmd += ["--record-iq", "yes" if self.record_iq_var.get() else "no"]
        self.spawn_in_terminal(cmd)

    def run_update_tle(self):
        self.run_cmd([sys.executable, station.script_path("update_tle.py")])
        self.refresh()

    def show_schedule(self):
        self.run_cmd([sys.executable, station.script_path("show_queue.py")])

    def show_pass_log(self):
        self.run_cmd([sys.executable, station.script_path("show_pass_log.py")])

    def plan_passes_auto(self):
        hours = simpledialog.askinteger(
            "Plan passes", "Hours ahead to predict:", initialvalue=24, minvalue=1)
        if not hours:
            return
        self.run_cmd([sys.executable, station.script_path("plan_passes.py"), "--hours", str(hours)])

    def plan_passes_interactive(self):
        """--interactive prompts y/n per pass on stdin - same reasoning
        as run_passes.py, this needs a real terminal, not a captured
        subprocess the GUI can't feed keystrokes into."""
        hours = simpledialog.askinteger(
            "Plan passes (interactive)", "Hours ahead to predict:",
            initialvalue=24, minvalue=1)
        if not hours:
            return
        cmd = [sys.executable, station.script_path("plan_passes.py"), "--hours", str(hours), "--interactive"]
        if self.spawn_in_terminal(cmd):
            self.log(self.output.get("1.0", tk.END) +
                     "\nApprove/reject passes there, then use 'Show schedule' "
                     "here once you're done.")

    def toggle_pass_record_iq(self):
        """Interactive - lists the queue and prompts for pass number(s) and
        on/off/clear, same reasoning as everywhere else that reads from
        stdin: needs a real terminal, not a captured subprocess."""
        cmd = [sys.executable, station.script_path("toggle_pass_record_iq.py")]
        self.spawn_in_terminal(cmd)

    def _extra_output_form(self, parent):
        """Small modal sub-form for one extra_outputs entry. Returns a
        dict with name/protocol/block/port-or-address, or None if
        cancelled. Used by both the Edit dialog's 'Add output...' button."""
        win = tk.Toplevel(parent)
        win.title("Add extra output")
        win.resizable(False, False)
        win.transient(parent)
        win.grab_set()

        result = {}

        # gather every existing extra_output across every satellite, so a
        # new one (e.g. BY70-4's SSDV viewer connection) can start from
        # another satellite's already-working entry (e.g. ASRTU-1's)
        # instead of retyping protocol/block conventions from scratch
        templates = []  # (display_string, entry_dict)
        for s in load_satellites():
            for e in s.get("extra_outputs", []):
                if e.get("protocol") == "tcp_bridge":
                    detail = f"port {e.get('port')}, bridge {e.get('bridge_port')}"
                else:
                    detail = f"port {e.get('port')}"
                templates.append((f"{s['name']}: {e.get('name')} ({e.get('protocol')}, {detail})", e))

        ttk.Label(win, text="Copy from existing output:").grid(
            row=0, column=0, sticky="e", padx=(10, 4), pady=(10, 4))
        template_var = tk.StringVar(value="(none - start blank)")
        template_menu = ttk.OptionMenu(
            win, template_var, "(none - start blank)",
            "(none - start blank)", *[t[0] for t in templates])
        template_menu.grid(row=0, column=1, padx=(0, 10), pady=(10, 4), sticky="w")

        ttk.Label(win, text="Name (e.g. ssdv_viewer):").grid(
            row=1, column=0, sticky="e", padx=(10, 4), pady=4)
        name_entry = ttk.Entry(win, width=28)
        name_entry.grid(row=1, column=1, padx=(0, 10), pady=4)

        ttk.Label(win, text="Protocol:").grid(row=2, column=0, sticky="e",
                                               padx=(10, 4), pady=4)
        protocol_var = tk.StringVar(value="tcp_server")
        protocol_menu = ttk.OptionMenu(win, protocol_var, "tcp_server",
                                        "tcp_server", "tcp_client", "tcp_bridge")
        protocol_menu.grid(row=2, column=1, padx=(0, 10), pady=4, sticky="w")

        ttk.Label(win, text="Block name in .grc:").grid(
            row=3, column=0, sticky="e", padx=(10, 4), pady=4)
        block_entry = ttk.Entry(win, width=28)
        block_entry.insert(0, "network_socket_pdu_0")
        block_entry.grid(row=3, column=1, padx=(0, 10), pady=4)

        value_label = ttk.Label(win, text="Port:")
        value_label.grid(row=4, column=0, sticky="e", padx=(10, 4), pady=4)
        value_entry = ttk.Entry(win, width=28)
        value_entry.grid(row=4, column=1, padx=(0, 10), pady=4)

        bridge_port_label = ttk.Label(win, text="Bridge port (downstream connects here):")
        bridge_port_entry = ttk.Entry(win, width=28)

        def show_bridge_port_field(show):
            if show:
                bridge_port_label.grid(row=5, column=0, sticky="e", padx=(10, 4), pady=4)
                bridge_port_entry.grid(row=5, column=1, padx=(0, 10), pady=4)
            else:
                bridge_port_label.grid_remove()
                bridge_port_entry.grid_remove()

        def on_protocol_change(*_):
            if protocol_var.get() == "tcp_bridge":
                value_label.config(text="Port (the flowgraph's own TCP_SERVER):")
                value_entry.delete(0, tk.END)
                block_entry.delete(0, tk.END)
                block_entry.insert(0, "network_socket_pdu_0")
                show_bridge_port_field(True)
            else:
                value_label.config(text="Port:")
                value_entry.delete(0, tk.END)
                block_entry.delete(0, tk.END)
                block_entry.insert(0, "network_socket_pdu_0")
                show_bridge_port_field(False)
        protocol_var.trace_add("write", on_protocol_change)

        def on_template_change(*_):
            choice = template_var.get()
            match = next((e for label, e in templates if label == choice), None)
            if match is None:
                return
            # copies the template's shape (protocol, block, value) as a
            # starting point - leaves the name blank since that should
            # be specific to this satellite, not copied verbatim
            protocol_var.set(match.get("protocol", "tcp_server"))
            block_entry.delete(0, tk.END)
            block_entry.insert(0, match.get("block", ""))
            value_entry.delete(0, tk.END)
            value_entry.insert(0, str(match.get("port", "")))
            if match.get("protocol") == "tcp_bridge":
                bridge_port_entry.delete(0, tk.END)
                bridge_port_entry.insert(0, str(match.get("bridge_port", "")))
        template_var.trace_add("write", on_template_change)

        status = ttk.Label(win, text="", foreground="#a00", wraplength=280)
        status.grid(row=6, column=0, columnspan=2, padx=10)

        def ok():
            name = name_entry.get().strip()
            block = block_entry.get().strip()
            value = value_entry.get().strip()
            if not name or not block or not value:
                status.config(text="All fields are required.")
                return
            if protocol_var.get() == "tcp_bridge" and not bridge_port_entry.get().strip():
                status.config(text="Bridge port is required for tcp_bridge.")
                return
            result["name"] = name
            result["protocol"] = protocol_var.get()
            result["block"] = block
            result["port"] = value
            if protocol_var.get() == "tcp_bridge":
                result["bridge_port"] = bridge_port_entry.get().strip()
            win.destroy()

        btn_row = ttk.Frame(win)
        btn_row.grid(row=7, column=0, columnspan=2, pady=10)
        ttk.Button(btn_row, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Cancel", command=win.destroy).pack(side="left")

        name_entry.focus_set()
        win.wait_window()  # block until this sub-form closes
        return result if result else None

    def add_satellite_dialog(self):
        sats = load_satellites()
        suggested_prod, suggested_cons = suggest_next_ports(sats)

        win = tk.Toplevel(self)
        win.title("Add satellite")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()  # modal - avoids editing the table while this is open

        fields = {}

        def add_row(r, label, default=""):
            ttk.Label(win, text=label).grid(row=r, column=0, sticky="e",
                                             padx=(10, 4), pady=4)
            entry = ttk.Entry(win, width=30)
            entry.insert(0, default)
            entry.grid(row=r, column=1, padx=(0, 10), pady=4, sticky="w")
            return entry

        fields["name"] = add_row(0, "Name (e.g. GEOSCAN-7):")
        fields["norad"] = add_row(1, "NORAD id:")
        fields["freq"] = add_row(2, "Downlink frequency (Hz):")
        fields["min_elev"] = add_row(3, "Min elevation (deg):", "15")

        record_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(win, text="Recording-only (no decoder yet, just raw IQ)",
                         variable=record_only_var,
                         command=lambda: toggle_fields()).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

        fields["producer_port"] = add_row(5, "Producer port:", str(suggested_prod))
        fields["consumer_port"] = add_row(6, "Consumer port:", str(suggested_cons))
        ttk.Label(win, text="(suggested - only used for decode-and-relay satellites)",
                  foreground="#666", font=("", 8)).grid(
            row=7, column=0, columnspan=2, padx=10, sticky="w")

        # Only meaningful once Recording-only is checked: builds the .grc from
        # a template (new_record_only_satellite.py) instead of leaving that as
        # a separate manual step. Default matches the standalone tool's own
        # default exactly, so the GUI and the CLI never disagree about which
        # file a bare "Add" would use.
        # inside a station, scionx.grc is some other radio's flowgraph - never offer it; point at this
        # station's own template even if it isn't there yet (the tool then says exactly what to do)
        default_template = ("flowgraphs/_record_only_template.grc"
                            if os.path.exists("flowgraphs/_record_only_template.grc") or self.current_station
                            else "flowgraphs/scionx.grc")
        use_template_var = tk.BooleanVar(value=False)
        template_check = ttk.Checkbutton(
            win, text="Also build its .grc from a template (name/freq only differ)",
            variable=use_template_var, command=lambda: toggle_fields())
        template_check.grid(row=8, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 0))

        fields["template"] = add_row(9, "Template .grc:", default_template)
        record_iq_var = tk.BooleanVar(value=False)
        record_iq_check = ttk.Checkbutton(
            win, text="Also write record_iq_toggle: true explicitly (optional - a wired template is detected anyway)",
            variable=record_iq_var)
        record_iq_check.grid(row=10, column=0, columnspan=2, sticky="w", padx=10)

        def toggle_fields():
            ro = record_only_var.get()
            fields["producer_port"].config(state="disabled" if ro else "normal")
            fields["consumer_port"].config(state="disabled" if ro else "normal")
            template_check.config(state="normal" if ro else "disabled")
            if not ro:
                use_template_var.set(False)
            ut = ro and use_template_var.get()
            fields["template"].config(state="normal" if ut else "disabled")
            record_iq_check.config(state="normal" if ut else "disabled")

        toggle_fields()  # correct initial disabled/enabled state for every field above

        status_label = ttk.Label(win, text="", foreground="#a00", wraplength=340)
        status_label.grid(row=11, column=0, columnspan=2, padx=10, pady=(4, 0))

        button_row = ttk.Frame(win)
        button_row.grid(row=12, column=0, columnspan=2, pady=10)

        def submit():
            name = fields["name"].get().strip()
            norad = fields["norad"].get().strip()
            freq = fields["freq"].get().strip()
            min_elev = fields["min_elev"].get().strip() or "15"

            if not name or not norad or not freq:
                status_label.config(text="Name, NORAD, and frequency are all required.")
                return

            use_template = record_only_var.get() and use_template_var.get()
            if use_template:
                # --record-only isn't passed separately - this tool always
                # calls add_satellite.py --record-only internally, and
                # --yes skips its own confirmation prompt, which run_cmd
                # (built for non-interactive tools, same as add_satellite.py
                # itself) has no way to answer
                template = fields["template"].get().strip()
                tool_name = "new_record_only_satellite.py"
                args = [sys.executable, station.script_path(tool_name), "--name", name, "--norad", norad,
                        "--freq", freq, "--min-elev", min_elev,
                        "--template", template, "--yes"]
                if record_iq_var.get():
                    args += ["--record-iq-toggle"]
            else:
                tool_name = "add_satellite.py"
                args = [sys.executable, station.script_path(tool_name), "--name", name,
                        "--norad", norad, "--freq", freq, "--min-elev", min_elev]
                if record_only_var.get():
                    args += ["--record-only"]
                else:
                    prod = fields["producer_port"].get().strip()
                    cons = fields["consumer_port"].get().strip()
                    if prod:
                        args += ["--producer-port", prod]
                    if cons:
                        args += ["--consumer-port", cons]

            status_label.config(text=f"Running {tool_name}...", foreground="#000")
            win.update_idletasks()

            returncode, output = self.run_cmd(args)

            if returncode == 0:
                self.refresh()
                win.destroy()
            else:
                # stay open, keep every typed value exactly as it was -
                # this is the whole point of a real form instead of
                # chained popups: a failure doesn't throw away your input
                last_line = output.strip().splitlines()[-1] if output.strip() else \
                    f"{tool_name} failed (see the main output pane for details)"
                status_label.config(text=f"Failed: {last_line}", foreground="#a00")

        ttk.Button(button_row, text="Add", command=submit).pack(side="left", padx=4)
        ttk.Button(button_row, text="Cancel", command=win.destroy).pack(side="left")

        fields["name"].focus_set()

    def edit_satellite_dialog(self):
        name = self.selected_name()
        if not name:
            return
        sats = load_satellites()
        sat = next((s for s in sats if s["name"] == name), None)
        if not sat:
            return

        uses_relay = "producer_port" in sat or "consumer_port" in sat
        has_extra = bool(sat.get("extra_outputs"))

        win = tk.Toplevel(self)
        win.title(f"Edit {name}")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        fields = {}

        def add_row(r, label, default=""):
            ttk.Label(win, text=label).grid(row=r, column=0, sticky="e",
                                             padx=(10, 4), pady=4)
            entry = ttk.Entry(win, width=30)
            entry.insert(0, str(default))
            entry.grid(row=r, column=1, padx=(0, 10), pady=4, sticky="w")
            return entry

        ttk.Label(win, text=f"Editing: {name} (name itself can't be changed here)",
                  font=("", 9, "bold")).grid(row=0, column=0, columnspan=2,
                                              padx=10, pady=(10, 4), sticky="w")

        fields["norad"] = add_row(1, "NORAD id:", sat.get("norad", ""))
        fields["freq"] = add_row(2, "Downlink frequency (Hz):", sat.get("freq_hz", ""))
        fields["min_elev"] = add_row(3, "Min elevation (deg):", sat.get("min_elev_deg", 15))

        if uses_relay:
            fields["producer_port"] = add_row(4, "Producer port:", sat.get("producer_port", ""))
            fields["consumer_port"] = add_row(5, "Consumer port:", sat.get("consumer_port", ""))
        else:
            ttk.Label(win, text="(recording-only - no producer/consumer ports to edit)",
                      foreground="#666", font=("", 8)).grid(
                row=4, column=0, columnspan=2, padx=10, sticky="w")

        enabled_var = tk.BooleanVar(value=sat.get("enabled", True))
        ttk.Checkbutton(win, text="Enabled", variable=enabled_var).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

        ttk.Label(win, text="Extra outputs (direct connections, bypassing relay.py):",
                  font=("", 9)).grid(row=7, column=0, columnspan=2, padx=10,
                                      pady=(6, 2), sticky="w")
        extra_frame = ttk.Frame(win)
        extra_frame.grid(row=8, column=0, columnspan=2, padx=10, pady=(0, 4), sticky="w")

        extra_list = tk.Listbox(extra_frame, height=3, width=42)
        extra_list.pack(side="left")

        def refresh_extra_list():
            extra_list.delete(0, tk.END)
            for e in sat.get("extra_outputs", []):
                if e.get("protocol") == "tcp_bridge":
                    detail = f"port {e.get('port', '?')}, bridge {e.get('bridge_port', '?')}"
                else:
                    detail = f"port {e.get('port', '?')}"
                extra_list.insert(tk.END, f"{e.get('name', '?')}  ({e.get('protocol', '?')}, {detail})")

        refresh_extra_list()

        extra_btns = ttk.Frame(extra_frame)
        extra_btns.pack(side="left", padx=(6, 0), fill="y")

        def add_extra_output():
            nonlocal sat
            result = self._extra_output_form(win)
            if result is None:
                return
            args = [sys.executable, station.script_path("edit_satellite.py"), name,
                    "--extra-output-name", result["name"],
                    "--extra-output-protocol", result["protocol"],
                    "--extra-output-block", result["block"],
                    "--extra-output-port", result["port"]]
            if result["protocol"] == "tcp_bridge":
                args += ["--extra-output-bridge-port", result["bridge_port"]]
            returncode, output = self.run_cmd(args)
            if returncode == 0:
                sat = next(s for s in load_satellites() if s["name"] == name)
                refresh_extra_list()
            else:
                last_line = output.strip().splitlines()[-1] if output.strip() else "failed"
                messagebox.showerror("Failed to add extra output", last_line)

        def remove_extra_output():
            nonlocal sat
            sel = extra_list.curselection()
            if not sel:
                return
            entry = sat.get("extra_outputs", [])[sel[0]]
            if not messagebox.askyesno("Remove extra output",
                                        f"Remove '{entry.get('name')}' from {name}?"):
                return
            returncode, output = self.run_cmd([sys.executable, station.script_path("edit_satellite.py"), name,
                                                "--remove-extra-output", entry.get("name")])
            if returncode == 0:
                sat = next(s for s in load_satellites() if s["name"] == name)
                refresh_extra_list()
            else:
                last_line = output.strip().splitlines()[-1] if output.strip() else "failed"
                messagebox.showerror("Failed to remove extra output", last_line)

        ttk.Button(extra_btns, text="Add output...", command=add_extra_output).pack(fill="x")
        ttk.Button(extra_btns, text="Remove selected",
                   command=remove_extra_output).pack(fill="x", pady=(4, 0))

        ttk.Label(win, text="(extra output changes above apply immediately, "
                             "separately from Save below)",
                  foreground="#666", font=("", 8)).grid(
            row=9, column=0, columnspan=2, padx=10, sticky="w")

        status_label = ttk.Label(win, text="", foreground="#a00", wraplength=340)
        status_label.grid(row=10, column=0, columnspan=2, padx=10, pady=(4, 0))

        button_row = ttk.Frame(win)
        button_row.grid(row=11, column=0, columnspan=2, pady=10)

        def submit():
            args = [sys.executable, station.script_path("edit_satellite.py"), name]

            norad = fields["norad"].get().strip()
            if norad and int(norad) != sat.get("norad"):
                args += ["--norad", norad]

            freq = fields["freq"].get().strip()
            if freq and int(freq) != sat.get("freq_hz"):
                args += ["--freq", freq]

            min_elev = fields["min_elev"].get().strip()
            if min_elev and float(min_elev) != float(sat.get("min_elev_deg", 15)):
                args += ["--min-elev", min_elev]

            if uses_relay:
                prod = fields["producer_port"].get().strip()
                if prod and int(prod) != sat.get("producer_port"):
                    args += ["--producer-port", prod]
                cons = fields["consumer_port"].get().strip()
                if cons and int(cons) != sat.get("consumer_port"):
                    args += ["--consumer-port", cons]

            if enabled_var.get() != sat.get("enabled", True):
                args += ["--enabled" if enabled_var.get() else "--disabled"]

            if len(args) == 3:  # nothing but [python, script, name]
                win.destroy()
                return

            status_label.config(text="Running edit_satellite.py...", foreground="#000")
            win.update_idletasks()

            returncode, output = self.run_cmd(args)

            if returncode == 0:
                self.refresh()
                win.destroy()
            else:
                last_line = output.strip().splitlines()[-1] if output.strip() else \
                    "edit_satellite.py failed (see the main output pane for details)"
                status_label.config(text=f"Failed: {last_line}", foreground="#a00")

        ttk.Button(button_row, text="Save", command=submit).pack(side="left", padx=4)
        ttk.Button(button_row, text="Cancel", command=win.destroy).pack(side="left")


if __name__ == "__main__":
    try:
        app = GroundtrackGUI()
    except station.StationError as e:
        sys.exit(f"station: {e}")
    app.mainloop()
