#!/usr/bin/env python3
"""TamaPoke serial monitor / debugging console (tkinter GUI).

    python3 tools/debugger/debugger_gui.py                 # pick a port in the GUI
    python3 tools/debugger/debugger_gui.py --port COM5      # connect immediately

Three tabs:
  Monitor      -- the live serial console: connect, quick/dangerous commands,
                  a filterable/searchable log, a persistent alert banner for
                  resets and integrity findings, timestamped manual marks.
  Save Editor  -- decodes an EXPORT (from the device or from a saved file)
                  into an editable view of the live pet, party, box and the
                  petA/petB/plyA/plyB checkpoints, flags likely corruption
                  (duplicate creatures, an unconsumed checkpoint handover),
                  and can save the result to a file or send it back to the
                  device as a restore.
  Soak Graph   -- a live heap/heap_min line chart off the same HEALTH lines
                  already being logged, for a multi-hour/day soak test.

Built after a live debugging session where a broken NVS checkpoint on real
hardware ("save: pet checkpoint failed") turned out to be entangled with a
second problem: every plain serial connection opened against the board was
itself triggering a chip reset (`rst:0x15 USB_UART_CHIP_RESET`), because
opening a port toggles DTR/RTS by default and this board's native USB-CDC-JTAG
peripheral resets on that signal. Everything here is built around not causing
that, and around not missing it if it happens anyway.

Needs pyserial; auto-installs it into the current interpreter if missing.
tkinter ships with a standard CPython install on Windows/Mac; on Debian-family
Linux install python3-tk separately.
"""
import argparse
import csv
import queue as queue_module
import re
import struct
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tools/debugger
ROOT = HERE.parent.parent                       # repo root
LOG_DIR = HERE / "serial_logs"
BACKUP_DIR = ROOT / "backups"
BUGREPORT_DIR = ROOT / "bugreports"
DEX_H = ROOT / "dex.h"
ITEMS_H = ROOT / "items.h"

BUG_REPORT_LOG_LINES = 50

# UI-only slot counts: how many blank editable rows to offer for a
# party/box/rivals section whose NVS key doesn't exist yet (a save that has
# never banked anything, or never played LAN). Sourced from party.h/pet.h --
# unlike everything in tpsave.py, these do NOT need to track the firmware
# exactly, because the wire format stays self-describing (a blob's slot count
# is always derived from its own byte length, never restated). Getting one
# of these wrong just means offering slightly too few/many blank rows to
# edit, not encoding anything incorrectly.
PARTY_SLOTS_UI = 6
BOX_SLOTS_UI = 18
RIVAL_CAP_UI = 10

# How many queued serial events _pump_queue will process in one Tk tick
# before yielding back to the event loop. See _pump_queue's docstring.
# Measured ~4ms/line of real Text-widget + classification work, so 100 keeps
# a single tick under ~0.4s even during a reset storm dumping a big backlog.
MAX_QUEUE_ITEMS_PER_TICK = 100

sys.path.insert(0, str(HERE))
import tpsave  # noqa: E402  (after sys.path fix)


def _ensure_pyserial():
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        return
    except ImportError:
        pass
    print("debugger_gui: pyserial not found, installing it now...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pyserial"])
    except subprocess.CalledProcessError:
        sys.exit(
            "Could not auto-install pyserial. Install it manually with:\n"
            f"  {sys.executable} -m pip install pyserial"
        )
    import serial  # noqa: F401
    import serial.tools.list_ports  # noqa: F401


_ensure_pyserial()

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import serial
import serial.tools.list_ports

BAUD = 115200

QUICK_COMMANDS = ["STATS", "HEALTH", "PARTY", "EXPORT", "LS"]
DANGEROUS_COMMANDS = ["WIPE"]

QUICK_TIPS = {
    "STATS": "Full live-pet state: species, care stats, IVs, training, streak, medals.",
    "HEALTH": "Uptime, heap/heap_min, PSRAM, current screen, save health, NVS headroom.",
    "PARTY": "Lists the 6 party slots by species/level (not the live pet).",
    "EXPORT": "Prints the whole save as IMPORT hex lines. Auto-captured to backups/.",
    "LS": "Lists files on the SD card.",
    "WIPE": "FACTORY RESET. Erases NVS and reboots to a fresh game. Export first.",
}

# --- line classification -----------------------------------------------
RE_RESET = re.compile(r"rst:0x[0-9a-fA-F]+ \(([^)]+)\)")
RE_BOOT_REASON = re.compile(r"boot: reset=(\S+)")
RE_CRASH = re.compile(r"^CRASH:")
RE_SAVE_FAIL = re.compile(r"save: .*(failed|BROKEN|would not open)", re.IGNORECASE)
RE_NVS = re.compile(r"nvs (?:health|boot): used=(\d+) avail=(\d+) total=(\d+)")
RE_HEALTH = re.compile(r"up=(\d+)s heap=(\d+) min=(\d+)")
RE_EXPORT_HEADER = re.compile(r"# TamaPoke save, (\d+) bytes")
RE_IMPORT_LINE = re.compile(r"^IMPORT( .*)?$")


class ToolTip:
    """Minimal hover tooltip: a borderless Toplevel near the widget."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 4
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        try:
            self.tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background="#222933",
                 foreground="#e6e6e6", relief="solid", borderwidth=1, wraplength=340,
                 font=("Segoe UI", 9), padx=6, pady=4).pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Tail:
    """Holds one serial connection open for the life of the session. DTR/RTS
    are set low BEFORE opening and never touched again, specifically so this
    tool cannot be the cause of the board's next USB_UART_CHIP_RESET.

    EVERY blocking OS-level serial call -- open(), read(), write(), close()
    -- happens on this class's own background thread, never on the Tk thread.
    write_line() only ever pushes onto a thread-safe queue; the background
    loop drains it before each read. That's not just tidiness: this board has
    reset mid-session more than once tonight, and a write() to a port whose
    other end isn't currently draining can block indefinitely by default in
    pyserial (write_timeout is None unless set). Before this, EVERY quick
    command, Mark, and Send-to-device wrote directly from the Tk thread, so
    the instant the board was slow to answer, the whole GUI froze with it --
    not just the connection attempt this class's open()-side fix already
    covers. write_timeout below is a second backstop for the same class of
    bug, in case anything still reaches self.ser directly."""

    def __init__(self, port, baud, out_queue):
        self.port_name = port
        self.baud = baud
        self.out_queue = out_queue
        self.ser = None
        self.stop_flag = threading.Event()
        self.thread = None
        self.send_queue = queue_module.Queue()

    def start(self):
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.ser = serial.Serial()
            self.ser.port = self.port_name
            self.ser.baudrate = self.baud
            self.ser.timeout = 0.2
            self.ser.write_timeout = 2
            self.ser.dtr = False
            self.ser.rts = False
            self.ser.open()
        except Exception as e:
            self.out_queue.put(("open_failed", str(e)))
            return
        if self.stop_flag.is_set():
            # cancelled (user hit Disconnect) while open() was still blocking
            try:
                self.ser.close()
            except Exception:
                pass
            return
        self.out_queue.put(("opened", None))

        buf = b""
        try:
            while not self.stop_flag.is_set():
                # Drain anything queued to send BEFORE the read, and on this
                # background thread only -- never on Tk's.
                try:
                    while True:
                        line = self.send_queue.get_nowait()
                        self.ser.write((line + "\n").encode())
                except queue_module.Empty:
                    pass

                chunk = self.ser.read(4096)
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode(errors="replace").rstrip("\r")
                        self.out_queue.put(("line", text))
        except serial.SerialException as e:
            self.out_queue.put(("dropped", str(e)))
        finally:
            try:
                self.ser.close()
            except Exception:
                pass

    def write_line(self, text):
        """Thread-safe and non-blocking from any caller -- just enqueues.
        The actual serial write happens on the background thread above."""
        self.send_queue.put(text)

    def close(self):
        self.stop_flag.set()


def hex_from_export_text(text: str) -> bytes:
    """Pulls the concatenated hex payload out of a captured 'IMPORT <hex>'
    block (the exact shape both EXPORT's serial output and a saved backup
    file are in), and turns it back into raw bytes."""
    hexstr = "".join(
        line.strip()[len("IMPORT"):].strip()
        for line in text.splitlines()
        if line.strip().startswith("IMPORT")
    )
    return bytes.fromhex(hexstr)


def export_text_from_bytes(raw: bytes) -> str:
    """The inverse: chunks raw bytes into the same 'IMPORT <hex...>' line
    shape the firmware's EXPORT prints, ending with the bare commit line."""
    hexstr = raw.hex().upper()
    lines = [f"# TamaPoke save, {len(raw)} bytes. Paste this whole block back."]
    for i in range(0, len(hexstr), 96):
        lines.append("IMPORT " + hexstr[i:i + 96])
    lines.append("IMPORT")
    return "\n".join(lines) + "\n"


class App:
    def __init__(self, root, initial_port=None):
        self.root = root
        root.title("TamaPoke serial monitor")
        root.geometry("1040x720")

        self.q = queue_module.Queue()
        self.tail = None
        self.auto_reconnect = tk.BooleanVar(value=True)
        self.reset_count = 0
        self.reset_reasons = {}
        self.save_fail_count = 0
        self.last_nvs = None
        self.last_health = None
        self.connect_time = None
        self.log_file = None
        self.trend_writer = None
        self.trend_file = None
        self.health_trend = []   # list of (elapsed_seconds, heap, heap_min)

        self._collecting_export = False
        self._export_lines = []
        self.export_callback = None   # one-shot: called with raw bytes on a full EXPORT

        self.cmd_history = []
        self.cmd_hist_idx = None

        self.filter_pattern = None
        self._pending_lines = 0   # count of lines added while scrolled up

        self.alerts = []   # list of (severity, text) newest last

        self._build_ui(initial_port)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._pump_queue)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self, initial_port):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)

        mon_frame = ttk.Frame(nb)
        editor_frame = ttk.Frame(nb)
        graph_frame = ttk.Frame(nb)
        nb.add(mon_frame, text="Monitor")
        nb.add(editor_frame, text="Save Editor")
        nb.add(graph_frame, text="Soak Graph")

        self._build_monitor_tab(mon_frame, initial_port)
        self._build_editor_tab(editor_frame)
        self._build_graph_tab(graph_frame)

    # ----- Monitor tab -------------------------------------------------
    def _build_monitor_tab(self, root, initial_port):
        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")

        ttk.Label(top, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=initial_port or "")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=14)
        self._refresh_ports()
        self.port_combo.pack(side="left", padx=(4, 4))
        b = ttk.Button(top, text="Refresh", command=self._refresh_ports)
        b.pack(side="left")

        self.connect_btn = ttk.Button(top, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=(8, 8))
        ToolTip(self.connect_btn, "Opens the port with DTR/RTS held low, so connecting\n"
                                   "itself never triggers a USB_UART_CHIP_RESET.")

        ttk.Checkbutton(top, text="Auto-reconnect", variable=self.auto_reconnect).pack(
            side="left", padx=(0, 8))

        self.status_var = tk.StringVar(value="disconnected")
        ttk.Label(top, textvariable=self.status_var, foreground="#a04000").pack(
            side="left", padx=(8, 0))
        self.duration_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.duration_var, foreground="#666").pack(
            side="left", padx=(8, 0))

        bug_btn = ttk.Button(top, text="Create bug report", command=self._create_bug_report)
        bug_btn.pack(side="right")
        ToolTip(bug_btn, f"Packages the last {BUG_REPORT_LOG_LINES} log lines, a fresh save\n"
                          "dump (if connected) or whatever's loaded in the Save Editor, and\n"
                          "the session's reset/health counters into one commented text file\n"
                          "under bugreports/ -- meant to be shared as-is.")

        mark_btn = ttk.Button(top, text="Mark", command=self._do_mark)
        mark_btn.pack(side="right", padx=(0, 8))
        ToolTip(mark_btn, "Insert a timestamped note into the log right now --\n"
                           "use this the instant you see something on the physical\n"
                           "device, so it lines up with what serial was doing. (F8)")
        self.root.bind("<F8>", lambda e: self._do_mark())

        # alert banner, hidden until something worth surfacing happens
        self.banner_holder = ttk.Frame(root)
        self.banner_holder.pack(fill="x")
        self.banner = tk.Frame(self.banner_holder, bg="#5c1f1f")
        self.banner_var = tk.StringVar()
        self.banner_label = tk.Label(self.banner, textvariable=self.banner_var, bg="#5c1f1f",
                                      fg="white", anchor="w", justify="left", wraplength=900,
                                      font=("Segoe UI", 10, "bold"), padx=8, pady=4)
        self.banner_label.pack(side="left", fill="x", expand=True)
        ttk.Button(self.banner, text="dismiss", command=self._dismiss_banner).pack(
            side="right", padx=6)
        # self.banner is not packed into banner_holder yet -- _show_alert() does that

        stat = ttk.Frame(root, padding=(6, 2))
        stat.pack(fill="x")
        self.reset_var = tk.StringVar(value="resets seen: 0")
        self.nvs_var = tk.StringVar(value="nvs: --")
        self.health_var = tk.StringVar(value="health: --")
        ttk.Label(stat, textvariable=self.reset_var, foreground="#a04000").pack(side="left")
        ttk.Label(stat, textvariable=self.nvs_var).pack(side="left", padx=(16, 0))
        ttk.Label(stat, textvariable=self.health_var).pack(side="left", padx=(16, 0))

        # filter/search bar
        filt = ttk.Frame(root, padding=(6, 0))
        filt.pack(fill="x")
        ttk.Label(filt, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        filt_entry = ttk.Entry(filt, textvariable=self.filter_var, width=30)
        filt_entry.pack(side="left", padx=(4, 4))
        filt_entry.bind("<Return>", lambda e: self._apply_filter())
        ttk.Button(filt, text="Apply", command=self._apply_filter).pack(side="left")
        ttk.Button(filt, text="Clear", command=self._clear_filter).pack(side="left", padx=(4, 8))
        ttk.Button(filt, text="Clear log", command=self._clear_log_view).pack(side="left")
        ttk.Button(filt, text="Copy log", command=self._copy_log).pack(side="left", padx=(4, 0))
        self.jump_btn = ttk.Button(filt, text="", command=self._jump_to_end)
        # packed on demand once there is something to jump to

        self.log = scrolledtext.ScrolledText(
            root, wrap="none", font=("Consolas", 10), bg="#0d1117", fg="#c9d1d9")
        self.log.pack(fill="both", expand=True, padx=6, pady=(4, 4))
        self.log.tag_config("reset", foreground="#ff8c42", font=("Consolas", 10, "bold"))
        self.log.tag_config("fail", foreground="#ff5c5c", font=("Consolas", 10, "bold"))
        self.log.tag_config("crash", foreground="#ffffff", background="#7a0000")
        self.log.tag_config("nvs", foreground="#6ec6ff")
        self.log.tag_config("health", foreground="#8adf6f")
        self.log.tag_config("export", foreground="#c792ea")
        self.log.tag_config("mark", foreground="#ffd166", font=("Consolas", 10, "bold"))
        self.log.tag_config("ts", foreground="#5a6270")
        self.log.tag_config("hidden", elide=True)
        self.log.config(state="disabled")

        bottom = ttk.Frame(root, padding=6)
        bottom.pack(fill="x")
        self.cmd_var = tk.StringVar()
        entry = ttk.Entry(bottom, textvariable=self.cmd_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self._send(self.cmd_var.get()))
        entry.bind("<Up>", self._hist_up)
        entry.bind("<Down>", self._hist_down)
        self.cmd_entry = entry
        ttk.Button(bottom, text="Send", command=lambda: self._send(self.cmd_var.get())).pack(
            side="left", padx=(4, 8))
        for cmd in QUICK_COMMANDS:
            btn = ttk.Button(bottom, text=cmd, command=lambda c=cmd: self._send(c))
            btn.pack(side="left", padx=2)
            ToolTip(btn, QUICK_TIPS.get(cmd, ""))
        for cmd in DANGEROUS_COMMANDS:
            btn = ttk.Button(bottom, text=cmd, command=lambda c=cmd: self._send_dangerous(c))
            btn.pack(side="left", padx=(10, 2))
            ToolTip(btn, QUICK_TIPS.get(cmd, ""))

        if initial_port:
            self._toggle_connect()

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if not self.port_var.get() and ports:
            self.port_var.set(ports[0])

    # ----- connection lifecycle ----------------------------------------
    def _toggle_connect(self):
        if self.tail is not None:
            self._disconnect()
            return
        port = self.port_var.get().strip()
        if not port:
            self._append_system("no port selected")
            return
        self._connect(port)

    def _connect(self, port):
        # Opening happens on Tail's background thread (see its docstring) --
        # this call must return immediately so the GUI never blocks on it.
        self.tail = Tail(port, BAUD, self.q)
        self.tail.start()
        self.status_var.set(f"connecting to {port}...")
        self.connect_btn.config(text="Cancel")

    def _disconnect(self, *, silent=False):
        was_connecting = self.tail is not None and self.connect_time is None
        if self.tail:
            self.tail.close()
            self.tail = None
        self.connect_time = None
        self.duration_var.set("")
        self.status_var.set("disconnected")
        self.connect_btn.config(text="Connect")
        if not silent and not was_connecting:
            self._append_system("disconnected")

    def _on_close(self):
        self._disconnect(silent=True)
        self.root.destroy()

    def _handle_opened(self):
        """The background thread finished serial.Serial().open() -- now it's
        safe to touch the UI as 'connected'."""
        if not self.tail:
            return   # cancelled in the meantime; _disconnect() already cleaned up
        port = self.tail.port_name
        self.connect_time = time.time()
        self.status_var.set(f"connected: {port}")
        self.connect_btn.config(text="Disconnect")
        self._open_log_file(port)
        self._append_system(
            f"connected to {port} @ {BAUD} -- DTR/RTS held low, no reset-on-connect")

    def _handle_open_failed(self, reason):
        port = self.tail.port_name if self.tail else self.port_var.get()
        self.tail = None
        self.status_var.set("disconnected")
        self.connect_btn.config(text="Connect")
        self._append_system(f"could not open {port}: {reason}")

    # ----- sending -------------------------------------------------------
    def _send(self, text, record_history=True):
        text = text.strip()
        if not text or not self.tail:
            return
        self.tail.write_line(text)
        self._append_line(f"> {text}", tags=("ts",))
        if record_history and (not self.cmd_history or self.cmd_history[-1] != text):
            self.cmd_history.append(text)
        self.cmd_hist_idx = None
        self.cmd_var.set("")

    def _hist_up(self, _event):
        if not self.cmd_history:
            return
        if self.cmd_hist_idx is None:
            self.cmd_hist_idx = len(self.cmd_history) - 1
        elif self.cmd_hist_idx > 0:
            self.cmd_hist_idx -= 1
        self.cmd_var.set(self.cmd_history[self.cmd_hist_idx])
        return "break"

    def _hist_down(self, _event):
        if self.cmd_hist_idx is None:
            return
        if self.cmd_hist_idx < len(self.cmd_history) - 1:
            self.cmd_hist_idx += 1
            self.cmd_var.set(self.cmd_history[self.cmd_hist_idx])
        else:
            self.cmd_hist_idx = None
            self.cmd_var.set("")
        return "break"

    def _send_dangerous(self, cmd):
        if not self.tail:
            return
        if not messagebox.askyesno(
                "Confirm", f"Send {cmd}?\n\n{QUICK_TIPS.get(cmd, '')}\n\n"
                           f"Make sure you have a fresh EXPORT backup first."):
            return
        self._send(cmd)

    def _do_mark(self):
        note = simpledialog.askstring("Mark", "Optional note (what did you just see?):",
                                       parent=self.root) or ""
        text = f"MARK: {note}" if note else "MARK"
        self._append_line(text, tags=("mark",))

    # ----- bug report -----------------------------------------------------
    def _create_bug_report(self):
        """Packages the last BUG_REPORT_LOG_LINES log lines, a save dump, and
        this session's counters into one commented text file meant to be
        shared as-is. If connected, captures a FRESH EXPORT first (most
        useful); otherwise falls back to whatever is already loaded in the
        Save Editor tab, or notes plainly that there is none."""
        if self.tail:
            self._append_system("bug report: capturing a fresh EXPORT first...")
            self.request_export(self._write_bug_report)
        else:
            self._write_bug_report(None)

    def _write_bug_report(self, fresh_raw):
        all_lines = [ln for ln in self.log.get("1.0", "end").splitlines() if ln.strip()]
        last_lines = all_lines[-BUG_REPORT_LOG_LINES:]

        decoded, source = None, None
        if fresh_raw is not None:
            decoded = tpsave.decode_save(fresh_raw, dex_h_path=str(DEX_H), items_h_path=str(ITEMS_H))
            source = "fresh EXPORT, captured for this report"
        elif self.loaded is not None:
            decoded = self.loaded
            source = "previously loaded save (Save Editor tab) -- NOT freshly captured"

        out = []
        out.append("=" * 78)
        out.append("TAMAPOKE DEBUGGER BUG REPORT")
        out.append(f"generated: {datetime.now().isoformat(timespec='seconds')}")
        out.append("=" * 78)
        out.append("")
        out.append("# Meant to be shared as-is. Every section below says what it is and")
        out.append("# why it's here, so it reads cold with no other context.")
        out.append("")

        out.append("## SESSION " + "-" * 68)
        out.append("# Port, how long it had been connected, and this tool's running")
        out.append("# counters since connecting.")
        out.append(f"port: {self.port_var.get() or '(not connected)'}")
        if self.connect_time:
            secs = int(time.time() - self.connect_time)
            out.append(f"connected for: {secs // 60}m{secs % 60:02d}s")
        else:
            out.append("connected for: not connected right now")
        out.append(f"resets seen this session: {self.reset_count} "
                    f"({self._reset_breakdown() or 'none'})")
        out.append(f"'save failed' lines seen this session: {self.save_fail_count}")
        if self.last_nvs:
            used, avail, total = self.last_nvs
            out.append(f"last NVS headroom seen: used={used} avail={avail} total={total}")
        if self.last_health:
            up, heap, mn = self.last_health
            out.append(f"last HEALTH seen: up={up}s heap={heap} min={mn}")
        if self.health_trend:
            first_heap, last_heap = self.health_trend[0][1], self.health_trend[-1][1]
            direction = "DOWN" if last_heap < first_heap else "flat/up"
            out.append(f"heap trend this session: {first_heap} -> {last_heap} over "
                        f"{len(self.health_trend)} HEALTH readings ({direction} "
                        f"{abs(last_heap - first_heap)} bytes)")
        out.append("")

        out.append(f"## LAST {len(last_lines)} SERIAL LOG LINES " + "-" * 46)
        out.append("# Verbatim, in order, timestamps included. The full session log is")
        out.append("# also kept under tools/debugger/serial_logs/ if more is ever needed.")
        out.extend(last_lines if last_lines else ["(no log lines captured yet)"])
        out.append("")

        out.append("## SAVE / MEMORY STATE " + "-" * 56)
        if decoded is None:
            out.append("# No save data available -- not connected, and nothing was")
            out.append("# previously loaded in the Save Editor tab.")
        else:
            out.append(f"# Source: {source}")
            if not decoded.get("ok"):
                out.append(f"# COULD NOT DECODE: {decoded.get('error')}")
            else:
                out.extend(self._describe_decoded_save(decoded))

        out.append("")
        out.append("=" * 78)
        out.append("END OF REPORT")
        out.append("=" * 78)

        BUGREPORT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        path = BUGREPORT_DIR / f"bugreport-{stamp}.txt"
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        self._append_system(f"bug report written -> {path.relative_to(ROOT)}")
        messagebox.showinfo("Bug report", f"Wrote:\n{path}")

    @staticmethod
    def _describe_decoded_save(decoded):
        names = decoded["names"]
        live = decoded["live_pet"]
        out = ["", "# The live pet, from the legacy scalar keys (what EXPORT/IMPORT carry):",
               f"  dex={live.get('dexn')} age_minutes={live.get('age')} "
               f"iv={live.get('ivat')}/{live.get('ivdf')}/{live.get('ivsp')}/{live.get('ivhp')} "
               f"tr={live.get('tatk')}/{live.get('tdef')}/{live.get('tspe')} "
               f"bond={live.get('bond')} nick={live.get('nick')!r}"]

        ck = decoded.get("pet_checkpoint")
        out += ["", "# The pet checkpoint (petA/petB) -- what the firmware ACTUALLY boots",
                "# from, which can disagree with the legacy scalars above if something's wrong:"]
        if ck and ck.get("valid"):
            out.append(f"  gen={ck['generation']} dex={ck['speciesId']} age_minutes={ck['ageMinutes']} "
                       f"iv={ck['ivAtk']}/{ck['ivDef']}/{ck['ivSpe']}/{ck['ivHp']} "
                       f"tr={ck['trAtk']}/{ck['trDef']}/{ck['trSpe']}")
            em = ck.get("ended_mon")
            if em and not em.get("empty"):
                out.append(f"  checkpoint tail (unconsumed handover): "
                           f"{tpsave.species_name(em['dex'], names)} lv{em['level']}")
        else:
            out.append(f"  INVALID or absent: {ck.get('error') if ck else 'no petA/petB present'}")

        out += ["", "# Party (6 slots, separate from the live pet above):"]
        party_rows = [
            f"  [{i}] {tpsave.species_name(m['dex'], names)} lv{m['level']} "
            f"iv={m['ivAtk']}/{m['ivDef']}/{m['ivSpe']}/{m['ivHp']} "
            f"tr={m['trAtk']}/{m['trDef']}/{m['trSpe']} age={m['ageMinutes']} nick={m['nick']!r}"
            for i, m in enumerate(decoded["party"]) if not m.get("empty")
        ]
        out += party_rows if party_rows else ["  (empty)"]

        out += ["", "# Box:"]
        box_rows = [f"  [{i}] {tpsave.species_name(m['dex'], names)} lv{m['level']}"
                    for i, m in enumerate(decoded["box"]) if not m.get("empty")]
        out += box_rows if box_rows else ["  (empty)"]

        p = decoded.get("player", {})
        out += ["", "# Player (outlives the pet): trainer name, badges, streak, medals:",
                f"  name={p.get('tnam')!r} region={p.get('reg')} "
                f"badges: kanto easy=0x{p.get('badg', 0):04X} hard=0x{p.get('badh', 0):04X} "
                f"streak={p.get('strk')} (best {p.get('bstrk')}) "
                f"medals={p.get('medal')} (lifetime {p.get('tmedal')}) "
                f"wallet=${p.get('wlt', 0)} (lifetime steps {p.get('stps', 0)})"]

        item_names = decoded.get("item_names", {})
        out += ["", "# Bag (non-empty stacks):"]
        bag_rows = [f"  {tpsave.item_name(s['key'], item_names)}: {s['count']}"
                    for s in decoded.get("bag", []) if s["count"] > 0]
        out += bag_rows if bag_rows else ["  (empty)"]

        out += ["", "# LAN rivals (opponents faced):"]
        rival_rows = [f"  {r['name'] or '(no name)'} [{r['mac']}]: {r['wins']}W-{r['losses']}L"
                      for r in decoded.get("rivals", []) if not r.get("empty")]
        out += rival_rows if rival_rows else ["  (none yet)"]

        out += ["", "# Automated integrity findings (duplicate creatures, an unconsumed",
                "# checkpoint handover, legacy-vs-checkpoint disagreement):"]
        findings = decoded.get("findings", [])
        out += [f"  [{f['severity']}] {f['message']}" for f in findings] if findings else ["  none detected"]
        return out

    # ----- queue pump (Tk thread) ---------------------------------------
    def _pump_queue(self):
        """Runs on a fixed 80ms tick, but the queue can arrive with a large
        backlog already waiting -- the board keeps running and printing
        continuously (deliberately: it's never reset just to be watched), so
        whatever it sent before this tool connected, or during a burst of
        resets, is sitting in the OS buffer the instant the port opens. That
        used to be drained in one unbounded `while True` loop -- hundreds of
        lines processed, and hundreds of Text-widget inserts and canvas
        redraws done, before Tk's event loop got to run even once. That is
        exactly what a GUI freeze looks like from the outside, and it lines
        up with the freeze reports: instant if the backlog was already
        there, up to the next tick or a later burst otherwise.

        Capped per tick now, and the rest is picked up on the tick after --
        Tk gets to repaint and respond between chunks either way."""
        self._graph_dirty = False
        processed = 0
        try:
            while processed < MAX_QUEUE_ITEMS_PER_TICK:
                kind, payload = self.q.get_nowait()
                processed += 1
                if kind == "line":
                    self._handle_line(payload)
                elif kind == "dropped":
                    self._handle_drop(payload)
                elif kind == "opened":
                    self._handle_opened()
                elif kind == "open_failed":
                    self._handle_open_failed(payload)
        except queue_module.Empty:
            pass
        if self._graph_dirty:
            self._redraw_graph()
        if self.connect_time:
            secs = int(time.time() - self.connect_time)
            self.duration_var.set(f"connected {secs // 60}m{secs % 60:02d}s")
        # Backlog still waiting -- come back almost immediately instead of
        # waiting out the normal 80ms, so a big burst drains quickly without
        # ever doing it all in one blocking pass.
        self.root.after(1 if processed >= MAX_QUEUE_ITEMS_PER_TICK else 80, self._pump_queue)

    def _handle_drop(self, reason):
        self._append_system(f"port dropped ({reason})")
        if self.tail:
            self.tail.close()
            self.tail = None
        self.connect_time = None
        self.status_var.set("disconnected (dropped)")
        self.connect_btn.config(text="Connect")
        if self.auto_reconnect.get():
            self._append_system("auto-reconnect armed, retrying in 2s...")
            self.root.after(2000, self._try_reconnect)

    def _try_reconnect(self):
        port = self.port_var.get().strip()
        if not port or self.tail is not None:
            return
        self._append_system(f"reconnecting to {port}...")
        self._connect(port)
        if self.tail is None and self.auto_reconnect.get():
            self.root.after(2000, self._try_reconnect)

    # ----- line classification + display --------------------------------
    def _handle_line(self, text):
        if self.log_file:
            self.log_file.write(f"{datetime.now().isoformat(timespec='milliseconds')} {text}\n")
            self.log_file.flush()

        tags = ()
        m = RE_RESET.search(text)
        if m:
            self.reset_count += 1
            reason = m.group(1)
            self.reset_reasons[reason] = self.reset_reasons.get(reason, 0) + 1
            self.reset_var.set(f"resets seen: {self.reset_count} ({self._reset_breakdown()})")
            tags = ("reset",)
            self._show_alert("warning", f"reset detected: {reason} (total {self.reset_count})")
        elif RE_BOOT_REASON.search(text):
            tags = ("reset",)
        elif RE_CRASH.match(text):
            tags = ("crash",)
            self._show_alert("error", f"crash breadcrumb: {text}")
        elif RE_SAVE_FAIL.search(text):
            self.save_fail_count += 1
            tags = ("fail",)

        m = RE_NVS.search(text)
        if m:
            used, avail, total = (int(x) for x in m.groups())
            self.last_nvs = (used, avail, total)
            self.nvs_var.set(f"nvs: used={used} avail={avail} total={total}")
            if total and avail / total < 0.1:
                self._show_alert("warning", f"NVS headroom low: avail={avail}/{total}")
            tags = tags or ("nvs",)

        m = RE_HEALTH.search(text)
        if m:
            up, heap, mn = (int(x) for x in m.groups())
            self.last_health = (up, heap, mn)
            self.health_var.set(f"health: up={up}s heap={heap} min={mn}")
            elapsed = (time.time() - self.connect_time) if self.connect_time else up
            self.health_trend.append((elapsed, heap, mn))
            self._trend_row(heap=heap, heap_min=mn)
            # NOT redrawn here -- a burst of many HEALTH lines processed in
            # one _pump_queue call would otherwise redraw the whole canvas
            # once per line. _pump_queue redraws at most once per tick.
            self._graph_dirty = True
            tags = tags or ("health",)

        self._track_export(text)
        if self._collecting_export:
            tags = ("export",)

        self._append_line(text, tags=tags)

    def _reset_breakdown(self):
        return ", ".join(f"{k}:{v}" for k, v in sorted(self.reset_reasons.items()))

    def _append_line(self, text, tags=()):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.config(state="normal")
        line_start = self.log.index("end-1c")
        self.log.insert("end", f"[{ts}] ", ("ts",))
        self.log.insert("end", text + "\n", tags)
        if self.filter_pattern is not None and not self.filter_pattern.search(text):
            self.log.tag_add("hidden", line_start, "end")
        self.log.config(state="disabled")
        if at_bottom:
            self.log.see("end")
        else:
            self._pending_lines += 1
            self.jump_btn.config(text=f"↓ {self._pending_lines} new")
            if not self.jump_btn.winfo_ismapped():
                self.jump_btn.pack(side="right")

    def _append_system(self, text):
        self._append_line(f"-- {text} --", tags=("reset",))

    def _jump_to_end(self):
        self.log.see("end")
        self._pending_lines = 0
        self.jump_btn.pack_forget()

    # ----- filter ---------------------------------------------------------
    def _apply_filter(self):
        pat = self.filter_var.get().strip()
        try:
            self.filter_pattern = re.compile(pat, re.IGNORECASE) if pat else None
        except re.error as e:
            messagebox.showerror("Bad filter", str(e))
            return
        self.log.config(state="normal")
        self.log.tag_remove("hidden", "1.0", "end")
        if self.filter_pattern:
            for i, line in enumerate(self.log.get("1.0", "end").splitlines(), start=1):
                if not self.filter_pattern.search(line):
                    self.log.tag_add("hidden", f"{i}.0", f"{i}.end+1c")
        self.log.config(state="disabled")

    def _clear_filter(self):
        self.filter_var.set("")
        self.filter_pattern = None
        self.log.config(state="normal")
        self.log.tag_remove("hidden", "1.0", "end")
        self.log.config(state="disabled")

    def _clear_log_view(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self._append_system("log view cleared (the file on disk still has everything)")

    def _copy_log(self):
        text = self.log.get("1.0", "end")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # ----- alert banner -----------------------------------------------
    def _show_alert(self, severity, text):
        self.alerts.append((severity, text))
        colors = {"error": "#5c1f1f", "warning": "#5c4a1f", "info": "#1f3a5c"}
        color = colors.get(severity, "#5c1f1f")
        self.banner.config(bg=color)
        self.banner_label.config(bg=color)
        n = len(self.alerts)
        prefix = f"({n}) " if n > 1 else ""
        self.banner_var.set(f"⚠ {prefix}{text}")
        if not self.banner.winfo_ismapped():
            self.banner.pack(fill="x")

    def _dismiss_banner(self):
        self.alerts = []
        self.banner.pack_forget()

    # ----- EXPORT auto-capture ------------------------------------------
    def _track_export(self, text):
        m = RE_EXPORT_HEADER.match(text)
        if m:
            self._collecting_export = True
            self._export_lines = [text]
            return
        if not self._collecting_export:
            return
        if RE_IMPORT_LINE.match(text):
            self._export_lines.append(text)
            if text.strip() == "IMPORT":
                self._finish_export()
        else:
            self._collecting_export = False
            self._export_lines = []

    def _finish_export(self):
        self._collecting_export = False
        block = "\n".join(self._export_lines) + "\n"
        self._export_lines = []
        BACKUP_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        path = BACKUP_DIR / f"export-{stamp}.txt"
        path.write_text(block, encoding="utf-8")
        self._append_system(f"EXPORT captured -> {path.relative_to(ROOT)}")
        try:
            raw = hex_from_export_text(block)
        except ValueError:
            raw = None
        if self.export_callback and raw is not None:
            cb, self.export_callback = self.export_callback, None
            cb(raw)

    def request_export(self, callback):
        """Sends EXPORT and calls callback(raw_bytes) once the block finishes.
        Used by the Save Editor's 'Load from device' and by the pre-restore
        safety backup."""
        if not self.tail:
            messagebox.showerror("Not connected", "Connect to a device first.")
            return
        self.export_callback = callback
        self._send("EXPORT")

    # ----- trend logging (CSV) ------------------------------------------
    def _open_log_file(self, port):
        LOG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        safe_port = re.sub(r"[^A-Za-z0-9]+", "_", port)
        self.log_file = open(LOG_DIR / f"{safe_port}-{stamp}.log", "a", encoding="utf-8")
        trend_path = LOG_DIR / f"{safe_port}-{stamp}-trend.csv"
        self.trend_file = open(trend_path, "a", newline="", encoding="utf-8")
        self.trend_writer = csv.writer(self.trend_file)
        self.trend_writer.writerow(["timestamp", "heap", "heap_min"])

    def _trend_row(self, heap="", heap_min=""):
        if not self.trend_writer:
            return
        self.trend_writer.writerow([datetime.now().isoformat(timespec="seconds"), heap, heap_min])
        self.trend_file.flush()

    # ================================================================ #
    #  Save Editor tab                                                  #
    # ================================================================ #
    def _build_editor_tab(self, root):
        self.loaded = None       # tpsave.decode_save() result
        self.edit_live = {}      # editable copy of live_pet legacy fields
        self.edit_party = []
        self.edit_box = []
        self.edit_bag = []
        self.edit_rivals = []
        self.dirty = False

        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        b1 = ttk.Button(top, text="Load from device", command=self._editor_load_from_device)
        b1.pack(side="left")
        ToolTip(b1, "Sends EXPORT and decodes the reply.")
        b2 = ttk.Button(top, text="Load from file...", command=self._editor_load_from_file)
        b2.pack(side="left", padx=(6, 0))
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=10)
        b3 = ttk.Button(top, text="Revert", command=self._editor_revert)
        b3.pack(side="left")
        b4 = ttk.Button(top, text="Save to file...", command=self._editor_save_to_file)
        b4.pack(side="left", padx=(6, 0))
        b5 = ttk.Button(top, text="Send to device", command=self._editor_send_to_device)
        b5.pack(side="left", padx=(6, 0))
        ToolTip(b5, "Backs up the device's CURRENT save first, then restores this one.\n"
                    "If you edited the live pet, the checkpoint keys are dropped so\n"
                    "your edit isn't silently overridden by a stale checkpoint on boot.")
        self.editor_summary_var = tk.StringVar(value="Nothing loaded yet.")
        ttk.Label(top, textvariable=self.editor_summary_var, foreground="#666").pack(
            side="left", padx=(14, 0))

        self.findings_box = tk.Text(root, height=4, bg="#1a1410", fg="#ffd166", wrap="word")
        self.findings_box.pack(fill="x", padx=6)
        self.findings_box.config(state="disabled")

        # Everything below is a lot of sections (live pet, player, party, box,
        # bag, rivals, checkpoints) -- taller than the window, so it scrolls
        # rather than clipping. Mousewheel is only bound while the pointer is
        # actually over this canvas, so it doesn't hijack scrolling on other
        # tabs' widgets.
        body_container = ttk.Frame(root)
        body_container.pack(fill="both", expand=True)
        canvas = tk.Canvas(body_container, highlightthickness=0)
        vsb = ttk.Scrollbar(body_container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        body = ttk.Frame(canvas, padding=6)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(body_id, width=e.width))
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # -- live pet form --
        live_frame = ttk.LabelFrame(body, text="Live pet (legacy scalars)")
        live_frame.pack(fill="x", pady=(0, 8))
        self.live_vars, next_row = self._build_scalar_grid(
            live_frame, tpsave.LIVE_PET_KEYS, tpsave.BOOL_LIVE_KEYS)
        self.live_vars["age"].trace_add("write", lambda *a: self._update_live_level())
        ttk.Label(live_frame, text="-> Level (derived from age; not stored separately)",
                  foreground="#888").grid(row=next_row, column=0, columnspan=6,
                                           sticky="w", padx=(6, 2), pady=(8, 2))
        self.live_level_var = tk.StringVar(value="?")
        ttk.Label(live_frame, textvariable=self.live_level_var, foreground="#8adf6f",
                  font=("Segoe UI", 10, "bold")).grid(row=next_row, column=6, sticky="w")

        # -- player (badges, streak, medals, settings -- outlives the pet) --
        player_frame = ttk.LabelFrame(
            body, text="Player (badges, streak, medals, minigame scores, Mart wallet, settings)")
        player_frame.pack(fill="x", pady=(0, 8))
        # 7 columns rather than the live-pet grid's default 4: PLAYER_KEYS is
        # 20 fields, and 7 is exactly what keeps it to 3 rows instead of
        # spilling an awkward near-empty 4th/5th row.
        self.player_vars, _ = self._build_scalar_grid(
            player_frame, tpsave.PLAYER_KEYS, tpsave.BOOL_PLAYER_KEYS, cols=7)

        # -- party / box --
        lists_frame = ttk.Frame(body)
        lists_frame.pack(fill="both", expand=True, pady=(0, 8))
        self.party_editor = self._build_mon_list_editor(lists_frame, "Party")
        self.box_editor = self._build_mon_list_editor(lists_frame, "Box")

        # -- bag / rivals --
        lists_frame2 = ttk.Frame(body)
        lists_frame2.pack(fill="both", expand=True)
        self.bag_tree = self._build_bag_editor(lists_frame2)
        self.rivals_tree = self._build_rivals_editor(lists_frame2)

        # -- checkpoint summary (read-only) --
        ck_frame = ttk.LabelFrame(body, text="Checkpoints (read-only -- what the firmware actually boots from)")
        ck_frame.pack(fill="x", pady=(8, 0))
        self.ck_text = tk.Text(ck_frame, height=5, bg="#0d1117", fg="#8adf6f", wrap="word")
        self.ck_text.pack(fill="x")
        self.ck_text.config(state="disabled")

    def _build_scalar_grid(self, parent, keys, bool_keys, cols=4):
        """Shared by the live-pet and player sections: a labeled grid of
        Entry/Checkbutton widgets, one per key, each wired to mark_dirty and
        tooltipped with its actual valid range. Returns (vars_dict, next
        free grid row) so a caller can add something below the grid."""
        vars_ = {}
        row = 0
        for i, key in enumerate(keys):
            row, c = divmod(i, cols)
            label_text = tpsave.label_for(key)
            ttk.Label(parent, text=label_text).grid(
                row=row, column=c * 2, sticky="e", padx=(6, 2), pady=2)
            if key in bool_keys:
                var = tk.BooleanVar()
                w = ttk.Checkbutton(parent, variable=var, command=self._mark_dirty)
                w.grid(row=row, column=c * 2 + 1, sticky="w", pady=2)
                ToolTip(w, label_text)
            else:
                var = tk.StringVar()
                w = ttk.Entry(parent, textvariable=var, width=10)
                w.grid(row=row, column=c * 2 + 1, sticky="w", pady=2)
                var.trace_add("write", lambda *a: self._mark_dirty())
                rng = tpsave.range_for_kind(tpsave.SAVE_FIELD_KIND[key])
                ToolTip(w, f"{label_text}\nvalid range: {rng[0]} to {rng[1]}" if rng else label_text)
            vars_[key] = var
        return vars_, row + 1

    def _build_bag_editor(self, parent):
        frame = ttk.LabelFrame(parent, text="Bag (inventory)")
        frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        cols = ("key", "item", "count")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (36, 130, 50)):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="center")
        tree.pack(fill="both", expand=True, side="top")
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Edit item...", command=self._edit_bag_item).pack(side="left")
        return tree

    def _refresh_bag_tree(self):
        tree = self.bag_tree
        tree.delete(*tree.get_children())
        names = self.loaded["item_names"] if self.loaded else {}
        for slot in self.edit_bag:
            if slot["key"] == 0:
                continue   # firmware's own unused filler slot
            tree.insert("", "end", iid=str(slot["key"]), values=(
                slot["key"], tpsave.item_name(slot["key"], names), slot["count"]))

    def _edit_bag_item(self):
        sel = self.bag_tree.selection()
        if not sel:
            messagebox.showinfo("Edit item", "Select an item first.")
            return
        key = int(sel[0])
        slot = next(s for s in self.edit_bag if s["key"] == key)
        names = self.loaded["item_names"] if self.loaded else {}
        dlg = tk.Toplevel(self.root)
        dlg.title(tpsave.item_name(key, names))
        ttk.Label(dlg, text="Count").grid(row=0, column=0, sticky="e", padx=8, pady=8)
        v = tk.StringVar(value=str(slot["count"]))
        ttk.Entry(dlg, textvariable=v, width=8).grid(row=0, column=1, sticky="w", padx=8, pady=8)

        def apply_and_close():
            raw = v.get().strip()
            try:
                val = int(raw)
            except ValueError:
                messagebox.showerror("Bad value", f"Count: '{raw}' is not a whole number.")
                return
            if not (0 <= val <= 255):
                messagebox.showerror("Out of range", "Count must be 0 to 255.")
                return
            slot["count"] = val
            self._mark_dirty()
            dlg.destroy()

        ttk.Button(dlg, text="Apply", command=apply_and_close).grid(row=1, column=0, pady=8)
        ttk.Button(dlg, text="Cancel", command=dlg.destroy).grid(row=1, column=1, pady=8)

    def _build_rivals_editor(self, parent):
        frame = ttk.LabelFrame(parent, text="LAN rivals (opponents faced, MAC-keyed)")
        frame.pack(side="left", fill="both", expand=True)
        cols = ("slot", "mac", "name", "wins", "losses")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (36, 120, 90, 50, 55)):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="center")
        tree.pack(fill="both", expand=True, side="top")
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Edit slot...", command=self._edit_rival_slot).pack(side="left")
        ttk.Button(btns, text="Clear slot", command=self._clear_rival_slot).pack(
            side="left", padx=(4, 0))
        return tree

    def _refresh_rivals_tree(self):
        tree = self.rivals_tree
        tree.delete(*tree.get_children())
        for i, r in enumerate(self.edit_rivals):
            if r.get("empty"):
                tree.insert("", "end", iid=str(i), values=(i, "-", "-", "", ""))
            else:
                tree.insert("", "end", iid=str(i),
                            values=(i, r["mac"], r["name"], r["wins"], r["losses"]))

    def _empty_rival(self):
        return {"mac": "000000000000", "name": "", "wins": 0, "losses": 0, "empty": True}

    def _padded_rivals(self, rivals, count):
        out = [dict(r) for r in rivals]
        while len(out) < count:
            out.append(self._empty_rival())
        return out

    def _padded_bag(self, slots, count):
        out = [dict(s) for s in slots]
        while len(out) < count:
            out.append({"key": len(out), "count": 0})
        return out

    def _clear_rival_slot(self):
        sel = self.rivals_tree.selection()
        if not sel:
            return
        self.edit_rivals[int(sel[0])] = self._empty_rival()
        self._mark_dirty()
        self._refresh_rivals_tree()

    def _edit_rival_slot(self):
        sel = self.rivals_tree.selection()
        if not sel:
            messagebox.showinfo("Edit slot", "Select a row first.")
            return
        idx = int(sel[0])
        r = self.edit_rivals[idx]
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Rival slot {idx}")
        fields = [("mac", "MAC (12 hex chars)", r["mac"]), ("name", "Name", r["name"]),
                  ("wins", "Wins", str(r["wins"])), ("losses", "Losses", str(r["losses"]))]
        vars_ = {}
        for i, (key, label, val) in enumerate(fields):
            ttk.Label(dlg, text=label).grid(row=i, column=0, sticky="e", padx=8, pady=4)
            v = tk.StringVar(value=val)
            ttk.Entry(dlg, textvariable=v, width=16).grid(row=i, column=1, sticky="w",
                                                           padx=8, pady=4)
            vars_[key] = v

        def apply_and_close():
            mac_raw = vars_["mac"].get().strip().upper().replace(":", "").replace(" ", "")
            if len(mac_raw) != 12 or any(c not in "0123456789ABCDEF" for c in mac_raw):
                messagebox.showerror("Bad value", "MAC must be exactly 12 hex characters.")
                return
            try:
                wins = int(vars_["wins"].get())
                losses = int(vars_["losses"].get())
            except ValueError:
                messagebox.showerror("Bad value", "Wins/losses must be whole numbers.")
                return
            if not (0 <= wins <= 65535 and 0 <= losses <= 65535):
                messagebox.showerror("Out of range", "Wins/losses must be 0 to 65535.")
                return
            self.edit_rivals[idx] = {
                "mac": mac_raw, "name": vars_["name"].get()[:11],
                "wins": wins, "losses": losses, "empty": mac_raw == "0" * 12,
            }
            self._mark_dirty()
            self._refresh_rivals_tree()
            dlg.destroy()

        ttk.Button(dlg, text="Apply", command=apply_and_close).grid(
            row=len(fields), column=0, pady=8)
        ttk.Button(dlg, text="Cancel", command=dlg.destroy).grid(
            row=len(fields), column=1, pady=8)

    def _build_mon_list_editor(self, parent, title):
        frame = ttk.LabelFrame(parent, text=title)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        cols = ("slot", "dex", "name", "level", "iv", "tr", "age", "nick")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=10)
        for c, w in zip(cols, (40, 40, 110, 45, 90, 70, 60, 80)):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="center")
        tree.pack(fill="both", expand=True, side="top")
        tree.tag_configure("dup", background="#5c1f1f")
        tree.tag_configure("edited", background="#3a3510")

        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Edit slot...", command=lambda: self._edit_mon_slot(title)).pack(
            side="left")
        ttk.Button(btns, text="Clear slot", command=lambda: self._clear_mon_slot(title)).pack(
            side="left", padx=(4, 0))
        return tree

    def _mon_list_for(self, title):
        return self.edit_party if title == "Party" else self.edit_box

    def _tree_for(self, title):
        return self.party_editor if title == "Party" else self.box_editor

    def _refresh_mon_tree(self, title):
        tree = self._tree_for(title)
        mons = self._mon_list_for(title)
        tree.delete(*tree.get_children())
        names = self.loaded["names"] if self.loaded else {}
        dup_keys = self._duplicate_identity_keys()
        for i, m in enumerate(mons):
            if m.get("empty"):
                tree.insert("", "end", iid=str(i), values=(i, "-", "-", "", "", "", "", ""))
                continue
            key = tpsave._identity(m)
            tag = "dup" if key in dup_keys else ()
            tree.insert("", "end", iid=str(i), values=(
                i, m["dex"], tpsave.species_name(m["dex"], names).split(" (")[0],
                m["level"], f"{m['ivAtk']}/{m['ivDef']}/{m['ivSpe']}/{m['ivHp']}",
                f"{m['trAtk']}/{m['trDef']}/{m['trSpe']}", m["ageMinutes"], m["nick"]),
                tags=(tag,) if tag else ())

    def _duplicate_identity_keys(self):
        """Identity tuples that appear more than once across live+party+box,
        for highlighting -- recomputed independently of find_integrity_issues()
        (which returns messages, not keys) but over the same identity tuple."""
        keys = {}
        live = self._current_live_identity()
        for rec in ([live] if live else []) + \
                   [m for m in self.edit_party if not m.get("empty")] + \
                   [m for m in self.edit_box if not m.get("empty")]:
            k = tpsave._identity(rec)
            if all(v == 0 for v in k):
                continue
            keys[k] = keys.get(k, 0) + 1
        return {k for k, n in keys.items() if n > 1}

    def _current_live_identity(self):
        try:
            age = int(self.live_vars["age"].get() or 0)
            return {
                "dex": int(self.live_vars["dexn"].get() or 0),
                "level": min(100, 1 + age // 60),
                "ivAtk": int(self.live_vars["ivat"].get() or 0),
                "ivDef": int(self.live_vars["ivdf"].get() or 0),
                "ivSpe": int(self.live_vars["ivsp"].get() or 0),
                "ivHp": int(self.live_vars["ivhp"].get() or 0),
                "trAtk": int(self.live_vars["tatk"].get() or 0),
                "trDef": int(self.live_vars["tdef"].get() or 0),
                "trSpe": int(self.live_vars["tspe"].get() or 0),
                "ageMinutes": age,
            }
        except (ValueError, KeyError):
            return None

    def _edit_mon_slot(self, title):
        tree = self._tree_for(title)
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Edit slot", "Select a row first.")
            return
        idx = int(sel[0])
        mons = self._mon_list_for(title)
        mon = dict(mons[idx]) if not mons[idx].get("empty") else self._empty_mon()
        self._open_mon_dialog(title, idx, mon)

    def _clear_mon_slot(self, title):
        tree = self._tree_for(title)
        sel = tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        mons = self._mon_list_for(title)
        mons[idx] = self._empty_mon()
        self._mark_dirty()
        self._refresh_mon_tree(title)

    @staticmethod
    def _empty_mon():
        return {"dex": 0, "level": 1, "medals": 0, "ivAtk": 0, "ivDef": 0, "ivSpe": 0,
                "ivHp": 0, "trAtk": 0, "trDef": 0, "trSpe": 0, "shiny": 0, "nick": "",
                "stateVersion": 0, "fullness": 80, "joy": 80, "energy": 80, "hygiene": 100,
                "poops": 0, "weight": 0, "bond": 0, "berryKnown": 0, "careMistakes": 0,
                "evoDeclinedLv": 0, "lastLearnLevel": 0, "ageMinutes": 0,
                "moves": [0, 0, 0, 0], "empty": True}

    @classmethod
    def _padded_mons(cls, mons, count):
        out = [dict(m) for m in mons]
        while len(out) < count:
            out.append(cls._empty_mon())
        return out

    def _open_mon_dialog(self, title, idx, mon):
        dlg = tk.Toplevel(self.root)
        dlg.title(f"{title} slot {idx}")
        numeric_fields = ["dex", "ageMinutes", "ivAtk", "ivDef", "ivSpe", "ivHp",
                          "trAtk", "trDef", "trSpe",
                          "fullness", "joy", "energy", "hygiene", "poops", "weight", "bond",
                          "berryKnown", "careMistakes", "evoDeclinedLv", "lastLearnLevel",
                          "stateVersion"]
        vars_ = {}
        row = 0
        for i, f in enumerate(numeric_fields):
            row, c = divmod(i, 2)
            label_text = tpsave.label_for(f)
            rng = tpsave.range_for_party_field(f)
            ttk.Label(dlg, text=label_text).grid(row=row, column=c * 2, sticky="e",
                                                  padx=(8, 2), pady=2)
            v = tk.StringVar(value=str(mon.get(f, 0)))
            entry = ttk.Entry(dlg, textvariable=v, width=12)
            entry.grid(row=row, column=c * 2 + 1, sticky="w", padx=(0, 8), pady=2)
            if rng:
                ToolTip(entry, f"{label_text}\nvalid range: {rng[0]} to {rng[1]}")
            vars_[f] = v
        row += 1

        shiny_var = tk.BooleanVar(value=bool(mon.get("shiny", 0)))
        ttk.Checkbutton(dlg, text="Shiny", variable=shiny_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=(8, 2), pady=2)
        ttk.Label(dlg, text="Nickname").grid(row=row, column=2, sticky="e", padx=(8, 2), pady=2)
        nick_var = tk.StringVar(value=mon.get("nick", ""))
        ttk.Entry(dlg, textvariable=nick_var, width=12).grid(
            row=row, column=3, sticky="w", padx=(0, 8), pady=2)
        row += 1

        level_var = tk.StringVar()

        def refresh_level(*_a):
            try:
                age = int(vars_["ageMinutes"].get() or 0)
                level_var.set(str(min(100, 1 + age // 60)))
            except ValueError:
                level_var.set("?")

        vars_["ageMinutes"].trace_add("write", refresh_level)
        refresh_level()
        ttk.Label(dlg, text="-> Level (derived from age; not stored separately)",
                  foreground="#888").grid(row=row, column=0, columnspan=2, sticky="w",
                                           padx=(8, 2), pady=(6, 2))
        ttk.Label(dlg, textvariable=level_var, foreground="#8adf6f",
                  font=("Segoe UI", 10, "bold")).grid(row=row, column=1, sticky="w")
        row += 1

        move_rng = tpsave.range_for_party_field("moveN")
        move_vars = []
        ttk.Label(dlg, text="Moves (index into MOVE_TBL)").grid(
            row=row, column=0, sticky="e", padx=(8, 2))
        mv_frame = ttk.Frame(dlg)
        mv_frame.grid(row=row, column=1, columnspan=3, sticky="w")
        for m in mon.get("moves", [0, 0, 0, 0]):
            v = tk.StringVar(value=str(m))
            e = ttk.Entry(mv_frame, textvariable=v, width=5)
            e.pack(side="left", padx=2)
            if move_rng:
                ToolTip(e, f"Move slot\nvalid range: {move_rng[0]} to {move_rng[1]}")
            move_vars.append(v)
        row += 1

        def apply_and_close():
            new_mon = {}
            for f in numeric_fields:
                raw = vars_[f].get().strip()
                label = tpsave.label_for(f)
                try:
                    val = int(raw)
                except ValueError:
                    messagebox.showerror("Bad value", f"{label}: '{raw}' is not a whole number.")
                    return
                rng = tpsave.range_for_party_field(f)
                if rng and not (rng[0] <= val <= rng[1]):
                    messagebox.showerror(
                        "Out of range",
                        f"{label}: {val} is out of range ({rng[0]} to {rng[1]}).")
                    return
                new_mon[f] = val

            names = self.loaded["names"] if self.loaded else {}
            if names and new_mon["dex"] not in (0,) and new_mon["dex"] not in names:
                if not messagebox.askyesno(
                        "Unknown species",
                        f"Dex #{new_mon['dex']} has no known name (dex.h only goes up to "
                        f"{max(names)}). It won't crash anything, but it will show as a bare "
                        f"number in-game. Use it anyway?"):
                    return

            moves = []
            for i, v in enumerate(move_vars):
                raw = v.get().strip()
                try:
                    mv = int(raw)
                except ValueError:
                    messagebox.showerror("Bad value", f"Move slot {i}: '{raw}' is not a whole number.")
                    return
                if move_rng and not (move_rng[0] <= mv <= move_rng[1]):
                    messagebox.showerror(
                        "Out of range",
                        f"Move slot {i}: {mv} is out of range ({move_rng[0]} to {move_rng[1]}).")
                    return
                moves.append(mv)

            new_mon["shiny"] = 1 if shiny_var.get() else 0
            new_mon["nick"] = nick_var.get()[:11]
            new_mon["moves"] = moves
            new_mon["medals"] = mon.get("medals", 0)
            new_mon["level"] = min(100, 1 + new_mon["ageMinutes"] // 60)
            new_mon["empty"] = new_mon["dex"] < 1
            self._mon_list_for(title)[idx] = new_mon
            self._mark_dirty()
            self._refresh_mon_tree(title)
            dlg.destroy()

        ttk.Button(dlg, text="Apply", command=apply_and_close).grid(
            row=row, column=0, columnspan=2, pady=8)
        ttk.Button(dlg, text="Cancel", command=dlg.destroy).grid(
            row=row, column=2, columnspan=2, pady=8)

    # ----- load / populate ----------------------------------------------
    def _editor_load_from_device(self):
        self.request_export(self._editor_populate)

    def _editor_load_from_file(self):
        path = filedialog.askopenfilename(
            initialdir=str(BACKUP_DIR) if BACKUP_DIR.exists() else str(ROOT),
            title="Load save export",
            filetypes=[("TamaPoke export", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
            raw = hex_from_export_text(text)
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        self._editor_populate(raw, source=path)

    def _editor_populate(self, raw, source="device"):
        decoded = tpsave.decode_save(raw, dex_h_path=str(DEX_H), items_h_path=str(ITEMS_H))
        if not decoded["ok"]:
            messagebox.showerror("Not a valid save", decoded["error"])
            return
        self.loaded = decoded
        self.edit_live = dict(decoded["live_pet"])
        # Padded up to the canonical slot count even when the underlying key
        # is absent (a save that never banked anything, or never played LAN
        # has no "box"/"rivals" key at all) -- otherwise there are zero rows
        # to select, and no way to add a first one through the UI.
        self.edit_party = self._padded_mons(decoded["party"], PARTY_SLOTS_UI)
        self.edit_box = self._padded_mons(decoded["box"], BOX_SLOTS_UI)
        self.edit_rivals = self._padded_rivals(decoded["rivals"], RIVAL_CAP_UI)
        item_slots = (max(decoded["item_names"]) + 1) if decoded["item_names"] else 16
        self.edit_bag = self._padded_bag(decoded["bag"], item_slots)
        self.dirty = False
        for key, var in self.live_vars.items():
            if isinstance(var, tk.BooleanVar):
                var.set(bool(self.edit_live.get(key, False)))
            else:
                var.set(str(self.edit_live.get(key, "")))
        for key, var in self.player_vars.items():
            if isinstance(var, tk.BooleanVar):
                var.set(bool(decoded["player"].get(key, False)))
            else:
                var.set(str(decoded["player"].get(key, "")))
        self._update_live_level()
        self._refresh_mon_tree("Party")
        self._refresh_mon_tree("Box")
        self._refresh_bag_tree()
        self._refresh_rivals_tree()
        self._refresh_checkpoint_view()
        self._refresh_findings()
        self.editor_summary_var.set(f"Loaded from {source} -- {decoded['field_count']} fields")

    def _update_live_level(self):
        try:
            age = int(self.live_vars["age"].get() or 0)
            self.live_level_var.set(str(min(100, 1 + age // 60)))
        except (ValueError, KeyError):
            self.live_level_var.set("?")

    def _refresh_checkpoint_view(self):
        d = self.loaded
        self.ck_text.config(state="normal")
        self.ck_text.delete("1.0", "end")
        if not d:
            self.ck_text.config(state="disabled")
            return
        names = d["names"]
        for label, key in (("petA", "petA"), ("petB", "petB"), ("plyA", "plyA"), ("plyB", "plyB")):
            ck = d.get(key)
            if ck is None:
                self.ck_text.insert("end", f"{label}: (absent)\n")
            elif not ck.get("valid"):
                self.ck_text.insert("end", f"{label}: INVALID -- {ck.get('error')}\n")
            elif "speciesId" in ck:
                em = ck.get("ended_mon")
                tail = ""
                if em and not em.get("empty"):
                    tail = f", tail handover: {tpsave.species_name(em['dex'], names)} lv{em['level']}"
                self.ck_text.insert(
                    "end", f"{label}: gen {ck['generation']}, "
                           f"{tpsave.species_name(ck['speciesId'], names)} lv{ck['level']}, "
                           f"age {ck['ageMinutes']}{tail}\n")
            else:
                self.ck_text.insert("end", f"{label}: gen {ck['generation']}, valid, "
                                            f"{ck['body_len']} tail bytes (not decoded)\n")
        self.ck_text.config(state="disabled")

    def _refresh_findings(self):
        live = self._current_live_identity() or {}
        state = {
            "live_pet": {**self.edit_live,
                         "dexn": live.get("dex", self.edit_live.get("dexn", 0)),
                         "age": live.get("ageMinutes", self.edit_live.get("age", 0))},
            "pet_checkpoint": self.loaded.get("pet_checkpoint") if self.loaded else None,
            "party": self.edit_party,
            "box": self.edit_box,
        }
        findings = tpsave.find_integrity_issues(state)
        self.findings_box.config(state="normal")
        self.findings_box.delete("1.0", "end")
        if not findings:
            self.findings_box.insert("end", "No integrity issues detected.")
        for f in findings:
            self.findings_box.insert("end", f"[{f['severity']}] {f['message']}\n")
        self.findings_box.config(state="disabled")

    def _mark_dirty(self):
        self.dirty = True
        self._refresh_mon_tree("Party")
        self._refresh_mon_tree("Box")
        self._refresh_bag_tree()
        self._refresh_rivals_tree()
        self._refresh_findings()

    def _editor_revert(self):
        if not self.loaded:
            return
        raw = tpsave.encode_export(self.loaded["fields"])
        self._editor_populate(raw, source="(reverted)")

    # ----- save / send ----------------------------------------------------
    @staticmethod
    def _encode_scalar_vars_into(fields, vars_dict):
        """Encodes one group of scalar StringVar/BooleanVar widgets (the
        live-pet form or the player form) into `fields`, in place. Raises
        tpsave.SaveError, naming the FRIENDLY field label, on anything that
        would not fit the field's actual on-disk width.

        A key the ORIGINAL save never had at all (some legacy keys are
        genuinely optional -- e.g. a save that never touched settings has no
        "lang" or "snd") populates at whatever default the display needed
        (False / "" / 0) rather than a value that was actually read. Writing
        that default back as if it were a real edit would be a silent,
        unintended change on a plain restore -- e.g. adding "snd": false
        for a device that never had a sound SETTING at all would newly
        disable sound on next boot. So: still absent AND still at that
        display default -> stays absent. Anything the user visibly changed
        (checked a box, typed a value) writes through normally."""
        for key, var in vars_dict.items():
            kind = tpsave.SAVE_FIELD_KIND[key]
            label = tpsave.label_for(key)
            was_absent = key not in fields
            if kind == tpsave.SK_BOOL:
                if was_absent and not var.get():
                    continue
                value = bool(var.get())
            elif kind == tpsave.SK_STR:
                value = var.get()
                if was_absent and value == "":
                    continue
            else:
                raw_str = var.get().strip()
                if was_absent and raw_str == "":
                    continue   # never had a value; still doesn't -- leave it out
                try:
                    value = int(raw_str)
                except ValueError:
                    raise tpsave.SaveError(f"{label}: '{raw_str}' is not a whole number")
                rng = tpsave.range_for_kind(kind)
                if rng and not (rng[0] <= value <= rng[1]):
                    raise tpsave.SaveError(
                        f"{label}: {value} is out of range ({rng[0]} to {rng[1]})")
            fields[key] = (kind, tpsave.encode_scalar(kind, value))

    def _rebuild_edited_fields(self):
        """Applies the editor's in-memory edits on top of the originally
        loaded field table, returning a new fields dict ready to encode.
        Never lets an oversized value reach struct.pack and crash -- caught
        as a clean tpsave.SaveError instead, everywhere it could occur.

        party/box/bag/rivals are padded with blank slots even when their key
        was absent (see _padded_mons/_padded_bag/_padded_rivals), so the same
        "don't fabricate a key that was never there" rule as
        _encode_scalar_vars_into applies here too: a key that was absent
        stays absent unless the padded slots actually hold real data now."""
        fields = OrderedDict(self.loaded["fields"])
        self._encode_scalar_vars_into(fields, self.live_vars)
        self._encode_scalar_vars_into(fields, self.player_vars)
        try:
            if "party" in fields or any(not m.get("empty") for m in self.edit_party):
                fields["party"] = (tpsave.SK_BYTES, tpsave.encode_mon_list(self.edit_party))
            if "box" in fields or any(not m.get("empty") for m in self.edit_box):
                fields["box"] = (tpsave.SK_BYTES, tpsave.encode_mon_list(self.edit_box))
            if "bag" in fields or any(s["count"] > 0 for s in self.edit_bag):
                fields["bag"] = (tpsave.SK_BYTES, tpsave.encode_bag(self.edit_bag))
            if "rivals" in fields or any(not r.get("empty") for r in self.edit_rivals):
                fields["rivals"] = (tpsave.SK_BYTES, tpsave.encode_rivals_blob(self.edit_rivals))
        except (struct.error, ValueError) as e:
            # Should be unreachable -- every dialog above validates its
            # fields' ranges before they land in the edit_* lists. Caught
            # anyway so a value that slipped in some other way is a dialog,
            # not a crash.
            raise tpsave.SaveError(f"a party/box/bag/rivals field is out of range: {e}")
        return fields

    def _editor_save_to_file(self):
        if not self.loaded:
            messagebox.showinfo("Save Editor", "Load a save first.")
            return
        try:
            fields = self._rebuild_edited_fields()
            raw = tpsave.encode_export(fields)
        except tpsave.SaveError as e:
            messagebox.showerror("Cannot encode", str(e))
            return
        path = filedialog.asksaveasfilename(
            initialdir=str(BACKUP_DIR), defaultextension=".txt",
            initialfile=f"edited-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.txt",
            filetypes=[("TamaPoke export", "*.txt")])
        if not path:
            return
        Path(path).write_text(export_text_from_bytes(raw), encoding="utf-8")
        messagebox.showinfo("Saved", f"Wrote {path}")

    def _editor_send_to_device(self):
        if not self.loaded:
            messagebox.showinfo("Save Editor", "Load a save first.")
            return
        if not self.tail:
            messagebox.showerror("Not connected", "Connect to a device first.")
            return
        try:
            fields = self._rebuild_edited_fields()
        except tpsave.SaveError as e:
            messagebox.showerror("Cannot encode", str(e))
            return

        drop_ckpt = self.dirty
        warn = ""
        if drop_ckpt:
            for k in ("petA", "petB", "plyA", "plyB"):
                fields.pop(k, None)
            warn = ("\n\nYou edited fields, so petA/petB/plyA/plyB will be OMITTED from the "
                    "restore -- this forces the firmware to rebuild fresh checkpoints from "
                    "your edited legacy values instead of a stale checkpoint silently "
                    "overriding them on next boot.")
        if not messagebox.askyesno(
                "Confirm restore",
                "This will back up the device's CURRENT save, then overwrite it with "
                f"the loaded/edited save.{warn}\n\nProceed?"):
            return

        try:
            raw = tpsave.encode_export(fields)
        except tpsave.SaveError as e:
            messagebox.showerror("Cannot encode", str(e))
            return

        def after_backup(_current_raw):
            threading.Thread(target=self._send_restore_blob, args=(raw,), daemon=True).start()

        self._append_system("backing up current device state before restoring...")
        self.request_export(after_backup)

    def _send_restore_blob(self, raw):
        lines = export_text_from_bytes(raw).splitlines()
        for line in lines:
            if line.startswith("IMPORT"):
                self.tail.write_line(line)
                time.sleep(0.01)
        self.root.after(0, lambda: self._append_system("restore sent -- watch for IMPORT OK / DONE"))

    # ================================================================ #
    #  Soak Graph tab                                                   #
    # ================================================================ #
    def _build_graph_tab(self, root):
        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Heap / heap_min over time (this session)").pack(side="left")
        ttk.Button(top, text="Clear graph", command=self._clear_graph).pack(side="right")

        self.graph_canvas = tk.Canvas(root, bg="#0d1117", highlightthickness=0)
        self.graph_canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.graph_canvas.bind("<Configure>", lambda e: self._redraw_graph())

    def _clear_graph(self):
        self.health_trend = []
        self._redraw_graph()

    def _redraw_graph(self):
        c = self.graph_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 20 or h < 20:
            return
        pad = 40
        if not self.health_trend:
            c.create_text(w // 2, h // 2, text="No HEALTH data yet this session",
                           fill="#666", font=("Segoe UI", 11))
            return
        xs = [p[0] for p in self.health_trend]
        heaps = [p[1] for p in self.health_trend]
        mins = [p[2] for p in self.health_trend]
        x0, x1 = min(xs), max(xs) or 1
        y0, y1 = min(mins + heaps), max(mins + heaps)
        if y1 == y0:
            y1 = y0 + 1
        if x1 == x0:
            x1 = x0 + 1

        def sx(x):
            return pad + (x - x0) / (x1 - x0) * (w - 2 * pad)

        def sy(y):
            return h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad)

        c.create_line(pad, h - pad, w - pad, h - pad, fill="#444")
        c.create_line(pad, pad, pad, h - pad, fill="#444")
        c.create_text(pad, h - pad + 14, text="0s", fill="#888", anchor="n")
        c.create_text(w - pad, h - pad + 14, text=f"{int(x1)}s", fill="#888", anchor="n")
        c.create_text(pad - 6, pad, text=str(y1), fill="#888", anchor="e")
        c.create_text(pad - 6, h - pad, text=str(y0), fill="#888", anchor="e")

        def poly(vals, color):
            if len(xs) < 2:
                c.create_oval(sx(xs[0]) - 2, sy(vals[0]) - 2, sx(xs[0]) + 2, sy(vals[0]) + 2,
                               fill=color, outline=color)
                return
            pts = []
            for x, v in zip(xs, vals):
                pts += [sx(x), sy(v)]
            c.create_line(*pts, fill=color, width=2, smooth=False)

        poly(heaps, "#8adf6f")
        poly(mins, "#ff8c42")
        c.create_text(w - pad, pad, text="heap", fill="#8adf6f", anchor="ne")
        c.create_text(w - pad, pad + 16, text="heap_min", fill="#ff8c42", anchor="ne")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="connect immediately to this port (e.g. COM5)")
    args = ap.parse_args()

    root = tk.Tk()
    App(root, initial_port=args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
