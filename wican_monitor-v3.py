"""
WiCAN Pro Live CAN Monitor
--------------------------
Windows desktop app (pure Python standard library: tkinter, no pip installs).
Connects to a WiCAN Pro running SLCAN over TCP and shows one row per CAN ID
with its latest 8 data bytes. Bytes that change are highlighted and fade out,
so you can see which byte reacts when you press a button / turn a knob.

BIT VIEW: click a row (or use the Up/Down arrow keys) to select an ID. The
panel at the bottom shows all 64 bits of that message, with the same change
highlighting per bit, plus optional per-bit toggle counters.

Colour legend (bytes and bits):
  orange (fading)  = changed in the last few seconds
  blue             = has changed at some point since last reset (dynamic)
  grey             = has NEVER changed since last reset (static)

Bit numbering in the bit view: each byte is shown MSB (7) on the left to
LSB (0) on the right. "Abs" bit number = byte*8 + bit (Intel/DBC style start bit).

MARKS AND LOG: type a label in the Mark box (e.g. "AC ON") and press the Mark
button (or Enter). The mark is timestamped and stored with the frame log.
"Export log" writes one CSV containing every logged frame and every mark in
time order. Columns: time, elapsed_s, event (FRAME or MARK), can_id, dlc,
data_hex, changed_bits, mark. changed_bits lists bits that differ from the
previous frame of that ID, as B<byte>.b<bit> (bit 0 = LSB), e.g. "B1.b3".
"Export filtered" does the same but keeps only the IDs in the ID filter box
(marks are always kept). The ID filter accepts several hex IDs separated by
commas or spaces, e.g. "215, 201"; the table shows the same IDs that the
filtered export writes.
By default only frames whose data changed are logged (small, easy to read);
untick "Log changes only" to log every frame.

Run:  python wican_monitor.py
"""

import csv
import heapq
import queue
import socket
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, ttk

DEFAULT_HOST = "192.168.113.184"
DEFAULT_PORT = 35001
LOG_MAX = 2_000_000   # max logged frames kept in memory (oldest dropped)

# (max age in seconds, tag) - how long a changed byte/bit stays highlighted
HEAT = [(0.5, "h0"), (1.5, "h1"), (3.0, "h2"), (6.0, "h3")]
HEATCOL = {
    "h0": ("#ff9800", "#000000"),
    "h1": ("#c77700", "#ffffff"),
    "h2": ("#7a4d00", "#ffffff"),
    "h3": ("#3d2a00", "#ffcc80"),
}
FADE_TOTAL = 6.0
FONT = ("Consolas", 11)
HEADER = (f"{'ID':<10}{'DLC':<4}{'Count':<9}{'Hz':<6}"
          + " ".join(f"B{i}" for i in range(8)) + "  ASCII")


def heat_tag(age):
    for limit, name in HEAT:
        if age < limit:
            return name
    return None


# --------------------------------------------------
# SLCAN PARSING
# --------------------------------------------------

def parse_slcan(line: bytes):
    """Return (can_id, extended, remote, dlc, data) or None."""
    if not line:
        return None
    t = chr(line[0])
    if t not in "tTrR":
        return None
    ext = t in "TR"
    rtr = t in "rR"
    idlen = 8 if ext else 3
    try:
        cid = int(line[1:1 + idlen], 16)
        dlc = int(chr(line[1 + idlen]), 16)
        if dlc > 8:
            return None
        data = b""
        if not rtr:
            hx = line[2 + idlen:2 + idlen + dlc * 2].decode("ascii")
            if len(hx) != dlc * 2:
                return None
            data = bytes.fromhex(hx)
        return cid, ext, rtr, dlc, data
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


# --------------------------------------------------
# NETWORK CONNECTION (background thread)
# --------------------------------------------------

class Connection:
    def __init__(self, frame_q, status_q):
        self.frame_q = frame_q
        self.status_q = status_q
        self.sock = None
        self.stop = threading.Event()
        self.thread = None

    def start(self, host, port):
        self.stop.clear()
        self.thread = threading.Thread(
            target=self._run, args=(host, port), daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        s = self.sock
        if s:
            try:
                s.sendall(b"C\r")
            except OSError:
                pass
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        self.sock = None

    def _run(self, host, port):
        try:
            self.status_q.put(("info", f"Connecting to {host}:{port} ..."))
            s = socket.create_connection((host, port), timeout=5)
            s.settimeout(0.5)
            self.sock = s
            s.sendall(b"O\r")  # open SLCAN channel
            self.status_q.put(("connected", f"Connected to {host}:{port}"))
        except OSError as e:
            self.status_q.put(("disconnected", f"Connection failed: {e}"))
            return

        buf = bytearray()
        reason = "Disconnected"
        while not self.stop.is_set():
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                if not self.stop.is_set():
                    reason = f"Connection error: {e}"
                break
            if not chunk:
                reason = "WiCAN closed the TCP connection"
                break
            buf.extend(chunk)
            while b"\r" in buf:
                line, _, rest = buf.partition(b"\r")
                buf = bytearray(rest)
                f = parse_slcan(bytes(line).strip())
                if f:
                    self.frame_q.put((time.monotonic(),) + f)
        self.status_q.put(("disconnected", reason))


# --------------------------------------------------
# DATA MODEL
# --------------------------------------------------

class Row:
    __slots__ = ("key", "can_id", "ext", "rtr", "dlc", "data", "count",
                 "last_count", "hz", "changed_at", "ever", "last_change",
                 "dirty", "bit_at", "bit_tog")

    def __init__(self, can_id, ext):
        self.key = (ext, can_id)
        self.can_id = can_id
        self.ext = ext
        self.rtr = False
        self.dlc = 0
        self.data = b""
        self.count = 0
        self.last_count = 0
        self.hz = 0
        self.changed_at = [-1e9] * 8
        self.ever = [False] * 8
        self.last_change = -1e9
        self.dirty = True
        self.bit_at = [-1e9] * 64     # last-change time per bit
        self.bit_tog = [0] * 64       # toggle count per bit

    def reset_history(self):
        self.ever = [False] * 8
        self.changed_at = [-1e9] * 8
        self.last_change = -1e9
        self.bit_at = [-1e9] * 64
        self.bit_tog = [0] * 64
        self.dirty = True

    @property
    def id_str(self):
        return f"{self.can_id:08X}" if self.ext else f"{self.can_id:03X}"


# --------------------------------------------------
# APPLICATION
# --------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title("WiCAN Pro Live CAN Monitor")
        root.geometry("1040x850")

        self.frame_q = queue.Queue()
        self.status_q = queue.Queue()
        self.conn = None
        self.connected = False
        self.paused = False

        self.rows = {}
        self.view = []          # rows currently displayed, in order
        self.view_index = {}    # key -> index in view
        self.needs_rebuild = False
        self.selected_key = None

        # marks and logging
        self.log = deque(maxlen=LOG_MAX)   # (ts, id, ext, rtr, dlc, data, prev)
        self.marks = []                    # (ts, text)
        self.log_last = {}                 # key -> last data seen
        self.wall_offset = time.time() - time.monotonic()
        self.last_log_status = 0.0

        self.total_frames = 0
        self.fps_count = 0
        self.fps = 0
        self.last_second = time.monotonic()

        self._build_ui()
        self.tick()

    # ---------- UI ----------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(side="top", fill="x")

        ttk.Label(top, text="Host:").pack(side="left")
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        ttk.Entry(top, textvariable=self.host_var, width=16).pack(
            side="left", padx=(2, 8))
        ttk.Label(top, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(top, textvariable=self.port_var, width=6).pack(
            side="left", padx=(2, 8))

        self.conn_btn = ttk.Button(top, text="Connect",
                                   command=self.toggle_connect)
        self.conn_btn.pack(side="left", padx=2)
        self.pause_btn = ttk.Button(top, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=2)
        ttk.Button(top, text="Reset highlights",
                   command=self.reset_highlights).pack(side="left", padx=2)
        ttk.Button(top, text="Clear all", command=self.clear_all).pack(
            side="left", padx=2)
        ttk.Button(top, text="Save CSV", command=self.save_csv).pack(
            side="left", padx=2)

        ttk.Label(top, text="  ID filter (hex):").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.request_rebuild())
        ttk.Entry(top, textvariable=self.filter_var, width=14).pack(side="left")

        # row 2: mark entry
        top2 = ttk.Frame(self.root, padding=(6, 0, 6, 2))
        top2.pack(side="top", fill="x")
        ttk.Label(top2, text="Mark:").pack(side="left")
        self.mark_var = tk.StringVar()
        self.mark_entry = ttk.Entry(top2, textvariable=self.mark_var, width=30)
        self.mark_entry.pack(side="left", padx=(2, 4))
        self.mark_entry.bind("<Return>", self.add_mark)
        ttk.Button(top2, text="Mark", command=self.add_mark).pack(
            side="left", padx=2)
        self.last_mark_var = tk.StringVar(value="Last mark: none")
        ttk.Label(top2, textvariable=self.last_mark_var).pack(
            side="left", padx=10)

        # row 3: log controls
        top3 = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        top3.pack(side="top", fill="x")
        self.reset_on_mark_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top3, text="Reset highlights on Mark",
                        variable=self.reset_on_mark_var).pack(side="left")
        self.log_changes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top3, text="Log changes only",
                        variable=self.log_changes_var).pack(
            side="left", padx=10)
        ttk.Button(top3, text="Export log", command=self.export_log).pack(
            side="left", padx=2)
        ttk.Button(top3, text="Export filtered",
                   command=self.export_filtered).pack(side="left", padx=2)
        ttk.Button(top3, text="Clear log", command=self.clear_log).pack(
            side="left", padx=2)
        self.log_status_var = tk.StringVar(value="Log: 0 frames, 0 marks")
        ttk.Label(top3, textvariable=self.log_status_var).pack(
            side="left", padx=10)

        # bottom widgets are packed first so the table gets the leftover space
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w",
                  padding=4).pack(side="bottom", fill="x")

        self._build_bit_panel()

        self.header = tk.Label(self.root, text=HEADER, font=FONT, anchor="w",
                               bg="#2d2d2d", fg="#ffffff")
        self.header.pack(side="top", fill="x")

        body = ttk.Frame(self.root)
        body.pack(side="top", fill="both", expand=True)
        self.text = tk.Text(body, font=FONT, bg="#1e1e1e", fg="#d4d4d4",
                            wrap="none", state="disabled", cursor="arrow",
                            borderwidth=0, highlightthickness=0)
        sb = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        t = self.text
        t.tag_configure("dim", foreground="#444444")
        t.tag_configure("static", foreground="#6e6e6e")
        t.tag_configure("dynamic", foreground="#4fc3f7")
        for name in ("h3", "h2", "h1", "h0"):  # later = higher priority
            bg, fg = HEATCOL[name]
            t.tag_configure(name, background=bg, foreground=fg)
        t.tag_configure("meta", foreground="#d4d4d4")
        t.tag_configure("selmeta", background="#094771", foreground="#ffffff")
        t.tag_configure("ascii", foreground="#9e9e9e")

        t.bind("<Button-1>", self.on_click)
        t.bind("<Up>", lambda e: self.move_selection(-1))
        t.bind("<Down>", lambda e: self.move_selection(1))

    def _build_bit_panel(self):
        panel = tk.Frame(self.root, bg="#252526", padx=8, pady=6)
        panel.pack(side="bottom", fill="x")

        head = tk.Frame(panel, bg="#252526")
        head.pack(fill="x")
        self.bit_title = tk.Label(
            head, text="Bit view: click a row above (or use Up/Down arrows)",
            font=("Consolas", 11, "bold"), bg="#252526", fg="#ffffff",
            anchor="w")
        self.bit_title.pack(side="left")
        self.counts_var = tk.BooleanVar(value=False)
        tk.Checkbutton(head, text="Show toggle counts",
                       variable=self.counts_var, bg="#252526", fg="#d4d4d4",
                       selectcolor="#1e1e1e", activebackground="#252526",
                       activeforeground="#ffffff").pack(side="right")

        grid = tk.Frame(panel, bg="#252526")
        grid.pack(anchor="w", pady=(4, 0))

        def hdr(text, col, w):
            tk.Label(grid, text=text, font=FONT, width=w, bg="#252526",
                     fg="#9e9e9e").grid(row=0, column=col, padx=1)

        hdr("Byte", 0, 5)
        for c, b in enumerate(range(7, -1, -1)):
            hdr(f"b{b}", 1 + c, 3)
        hdr("Hex", 9, 5)
        hdr("Dec", 10, 5)
        hdr("Bin", 11, 9)
        hdr("Abs bits", 12, 9)

        self.bit_cells = []     # [byte][bit_column] -> Label
        self.bit_cache = {}     # Label -> last (text, bg, fg)
        self.byte_labels = []   # per byte: (name, hex, dec, bin)
        for i in range(8):
            r = i + 1
            name = tk.Label(grid, text=f"B{i}", font=FONT, width=5,
                            bg="#252526", fg="#d4d4d4")
            name.grid(row=r, column=0, padx=1, pady=1)
            cells = []
            for c in range(8):
                lbl = tk.Label(grid, text="", font=FONT, width=3,
                               bg="#1e1e1e", fg="#444444")
                lbl.grid(row=r, column=1 + c, padx=1, pady=1)
                cells.append(lbl)
            self.bit_cells.append(cells)
            hx = tk.Label(grid, text="", font=FONT, width=5, bg="#252526",
                          fg="#d4d4d4")
            hx.grid(row=r, column=9)
            dc = tk.Label(grid, text="", font=FONT, width=5, bg="#252526",
                          fg="#d4d4d4")
            dc.grid(row=r, column=10)
            bn = tk.Label(grid, text="", font=FONT, width=9, bg="#252526",
                          fg="#9e9e9e")
            bn.grid(row=r, column=11)
            ab = tk.Label(grid, text=f"{i * 8}-{i * 8 + 7}", font=FONT,
                          width=9, bg="#252526", fg="#6e6e6e")
            ab.grid(row=r, column=12)
            self.byte_labels.append((name, hx, dc, bn))

    # ---------- actions ----------

    def toggle_connect(self):
        if self.connected or (self.conn and self.conn.thread
                              and self.conn.thread.is_alive()):
            self.conn.close()
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.status_var.set("Invalid port")
            return
        self.conn = Connection(self.frame_q, self.status_q)
        self.conn.start(self.host_var.get().strip(), port)
        self.conn_btn.config(text="Disconnect")

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(text="Resume" if self.paused else "Pause")

    def reset_highlights(self):
        for r in self.rows.values():
            r.reset_history()

    def clear_all(self):
        self.rows.clear()
        self.selected_key = None
        self.request_rebuild()

    def request_rebuild(self):
        self.needs_rebuild = True

    def filter_tokens(self):
        raw = self.filter_var.get().upper().replace(",", " ").replace(";", " ")
        out = []
        for tok in raw.split():
            if tok.startswith("0X"):
                tok = tok[2:]
            if tok:
                out.append(tok)
        return out

    def id_matches(self, id_str):
        tokens = self.filter_tokens()
        return not tokens or any(t in id_str for t in tokens)

    @staticmethod
    def fmt_id(can_id, ext):
        return f"{can_id:08X}" if ext else f"{can_id:03X}"

    def save_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "extended", "dlc", "count", "data_hex"])
            for key in sorted(self.rows):
                r = self.rows[key]
                w.writerow([r.id_str, r.ext, r.dlc, r.count, r.data.hex().upper()])
        self.status_var.set(f"Saved snapshot to {path}")

    # ---------- marks and logging ----------

    def log_frame(self, f):
        ts, can_id, ext, rtr, dlc, data = f
        key = (ext, can_id)
        prev = self.log_last.get(key)
        self.log_last[key] = data
        if self.log_changes_var.get() and prev is not None and prev == data:
            return
        self.log.append((ts, can_id, ext, rtr, dlc, data, prev))

    def wall_stamp(self, ts):
        return datetime.fromtimestamp(self.wall_offset + ts)

    def add_mark(self, event=None):
        text = self.mark_var.get().strip() or "MARK"
        ts = time.monotonic()
        self.marks.append((ts, text))
        stamp = self.wall_stamp(ts).strftime("%H:%M:%S.%f")[:-3]
        self.last_mark_var.set(f"Last mark: {stamp}  {text}")
        if self.reset_on_mark_var.get():
            self.reset_highlights()
        self.mark_entry.focus_set()
        self.mark_entry.select_range(0, "end")
        return "break"

    @staticmethod
    def changed_bits(prev, data):
        if prev is None:
            return "NEW"
        out = []
        for i in range(min(len(prev), len(data))):
            diff = prev[i] ^ data[i]
            for b in range(7, -1, -1):
                if (diff >> b) & 1:
                    out.append(f"B{i}.b{b}")
        return " ".join(out)

    def clear_log(self):
        self.log.clear()
        self.marks.clear()
        self.log_last.clear()
        self.last_mark_var.set("Last mark: none")
        self.log_status_var.set("Log: 0 frames, 0 marks")

    def export_filtered(self):
        self.export_log(filtered=True)

    def export_log(self, path=None, filtered=False):
        frames = list(self.log)
        marks = list(self.marks)
        if not frames and not marks:
            self.status_var.set("Nothing to export yet.")
            return
        # elapsed_s counts from the start of the whole log, so filtered and
        # unfiltered exports of the same session line up with each other
        first = min(frames[0][0] if frames else float("inf"),
                    marks[0][0] if marks else float("inf"))
        tag = ""
        if filtered:
            tokens = self.filter_tokens()
            if not tokens:
                self.status_var.set(
                    "Type one or more IDs in the ID filter first (e.g. 215, 201).")
                return
            frames = [f for f in frames
                      if self.id_matches(self.fmt_id(f[1], f[2]))]
            if not frames:
                self.status_var.set("No logged frames match the ID filter.")
                return
            ids = "-".join(t for t in tokens if t.isalnum())[:40]
            tag = f"_filtered_{ids}"
        if path is None:
            default = datetime.now().strftime(f"wican_log{tag}_%Y%m%d_%H%M%S.csv")
            path = filedialog.asksaveasfilename(
                defaultextension=".csv", initialfile=default,
                filetypes=[("CSV", "*.csv")])
            if not path:
                return
        mark_iter = ((m[0], 0, m) for m in marks)
        frame_iter = ((f[0], 1, f) for f in frames)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "elapsed_s", "event", "can_id", "dlc",
                        "data_hex", "changed_bits", "mark"])
            for ts, kind, item in heapq.merge(
                    mark_iter, frame_iter, key=lambda x: (x[0], x[1])):
                stamp = self.wall_stamp(ts).strftime(
                    "%Y-%m-%d %H:%M:%S.%f")[:-3]
                elapsed = f"{ts - first:.3f}"
                if kind == 0:
                    w.writerow([stamp, elapsed, "MARK", "", "", "", "",
                                item[1]])
                else:
                    _, can_id, ext, rtr, dlc, data, prev = item
                    cid = f"0x{can_id:08X}" if ext else f"0x{can_id:03X}"
                    hx = "RTR" if rtr else " ".join(f"{b:02X}" for b in data)
                    w.writerow([stamp, elapsed, "FRAME", cid, dlc, hx,
                                self.changed_bits(prev, data), ""])
        self.status_var.set(
            f"Exported {len(frames)} frames and {len(marks)} marks to {path}"
            + (f" (IDs: {', '.join(self.filter_tokens())})" if filtered else ""))

    # ---------- selection ----------

    def on_click(self, event):
        self.text.focus_set()
        idx = self.text.index(f"@{event.x},{event.y}")
        box = self.text.bbox(idx)
        if box is None or not (box[1] <= event.y < box[1] + box[3]):
            return "break"
        line = int(idx.split(".")[0])
        if 1 <= line <= len(self.view):
            self.select(self.view[line - 1])
        return "break"

    def move_selection(self, delta):
        if not self.view:
            return "break"
        cur = self.view_index.get(self.selected_key, -1)
        new = max(0, min(len(self.view) - 1, cur + delta if cur >= 0 else 0))
        self.select(self.view[new])
        self.text.see(f"{new + 1}.0")
        return "break"

    def select(self, row):
        old = self.selected_key
        self.selected_key = row.key if row else None
        for k in (old, self.selected_key):
            r = self.rows.get(k)
            if r is not None and r.key in self.view_index:
                self.redraw_row(r, time.monotonic())
        self.update_bit_panel(time.monotonic())

    # ---------- data handling ----------

    def apply_frame(self, ts, can_id, ext, rtr, dlc, data):
        key = (ext, can_id)
        row = self.rows.get(key)
        if row is None:
            row = Row(can_id, ext)
            self.rows[key] = row
            self.needs_rebuild = True
        else:
            old = row.data
            for i in range(min(len(old), len(data))):
                diff = old[i] ^ data[i]
                if diff:
                    row.changed_at[i] = ts
                    row.ever[i] = True
                    row.last_change = ts
                    for b in range(8):
                        if (diff >> b) & 1:
                            row.bit_at[i * 8 + b] = ts
                            row.bit_tog[i * 8 + b] += 1
        row.rtr = rtr
        row.dlc = dlc
        row.data = data
        row.count += 1
        row.dirty = True

    def rebuild_view(self):
        rows = [self.rows[k] for k in sorted(self.rows)]
        rows = [r for r in rows if self.id_matches(r.id_str)]
        self.view = rows
        self.view_index = {r.key: i for i, r in enumerate(rows)}
        y = self.text.yview()[0]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n" * len(rows))
        self.text.configure(state="disabled")
        for r in rows:
            r.dirty = True
        self.text.yview_moveto(y)
        self.needs_rebuild = False

    def redraw_row(self, row, now):
        self.text.configure(state="normal")
        self.render_row(row, now)
        self.text.configure(state="disabled")

    def render_row(self, row, now):
        n = self.view_index[row.key] + 1
        tx = self.text

        def put(s, tag):
            tx.insert(f"{n}.end", s, tag)

        tx.delete(f"{n}.0", f"{n}.end")
        meta_tag = "selmeta" if row.key == self.selected_key else "meta"
        put(f"{row.id_str:<10}{'RTR' if row.rtr else row.dlc:<4}"
            f"{row.count:<9}{row.hz:<6}", meta_tag)
        for i in range(8):
            if row.rtr or i >= row.dlc:
                put("   ", "dim")
                continue
            tag = heat_tag(now - row.changed_at[i])
            if tag is None:
                tag = "dynamic" if row.ever[i] else "static"
            put(f"{row.data[i]:02X}", tag)
            put(" ", "meta")
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row.data)
        put(" " + asc, "ascii")
        row.dirty = False

    # ---------- bit panel ----------

    def set_cell(self, lbl, text, bg, fg):
        style = (text, bg, fg)
        if self.bit_cache.get(lbl) != style:
            lbl.config(text=text, bg=bg, fg=fg)
            self.bit_cache[lbl] = style

    def update_bit_panel(self, now):
        row = self.rows.get(self.selected_key) if self.selected_key else None
        if row is None:
            self.bit_title.config(
                text="Bit view: click a row above (or use Up/Down arrows)")
            for i in range(8):
                for lbl in self.bit_cells[i]:
                    self.set_cell(lbl, "", "#1e1e1e", "#444444")
                _, hx, dc, bn = self.byte_labels[i]
                hx.config(text="")
                dc.config(text="")
                bn.config(text="")
            return

        self.bit_title.config(
            text=f"Bit view: ID 0x{row.id_str}   DLC {row.dlc}   "
                 f"msgs {row.count}")
        show_counts = self.counts_var.get()
        for i in range(8):
            _, hx, dc, bn = self.byte_labels[i]
            if row.rtr or i >= len(row.data):
                for lbl in self.bit_cells[i]:
                    self.set_cell(lbl, "", "#1e1e1e", "#444444")
                hx.config(text="")
                dc.config(text="")
                bn.config(text="")
                continue
            val = row.data[i]
            hx.config(text=f"{val:02X}")
            dc.config(text=str(val))
            bn.config(text=f"{val:08b}")
            for c in range(8):
                b = 7 - c
                idx = i * 8 + b
                bit = (val >> b) & 1
                tog = row.bit_tog[idx]
                tag = heat_tag(now - row.bit_at[idx])
                if tag:
                    bg, fg = HEATCOL[tag]
                elif tog:
                    bg, fg = ("#26384a" if bit else "#1e1e1e"), "#4fc3f7"
                else:
                    bg, fg = ("#3a3a3a" if bit else "#1e1e1e"), "#9e9e9e"
                if show_counts:
                    txt = str(tog) if tog < 100 else "99+"
                else:
                    txt = str(bit)
                self.set_cell(self.bit_cells[i][c], txt, bg, fg)

    # ---------- main loop ----------

    def tick(self):
        now = time.monotonic()

        # status messages from the network thread
        try:
            while True:
                kind, msg = self.status_q.get_nowait()
                self.status_var.set(msg)
                if kind == "connected":
                    self.connected = True
                elif kind == "disconnected":
                    self.connected = False
                    self.conn_btn.config(text="Connect")
        except queue.Empty:
            pass

        # incoming frames
        for _ in range(20000):
            try:
                f = self.frame_q.get_nowait()
            except queue.Empty:
                break
            self.log_frame(f)  # logging continues even while paused
            if self.paused:
                continue  # discard while paused so the display stays frozen
            self.total_frames += 1
            self.fps_count += 1
            self.apply_frame(*f)

        if not self.paused:
            # once-per-second rate calculation
            if now - self.last_second >= 1.0:
                self.fps = self.fps_count
                self.fps_count = 0
                self.last_second = now
                for r in self.rows.values():
                    r.hz = r.count - r.last_count
                    r.last_count = r.count
                    r.dirty = True
                if self.connected:
                    self.status_var.set(
                        f"Connected | {len(self.rows)} IDs | "
                        f"{self.fps} frames/s | {self.total_frames} total")

            if self.needs_rebuild:
                self.rebuild_view()

            self.text.configure(state="normal")
            for r in self.view:
                if r.dirty or (now - r.last_change) < FADE_TOTAL + 0.5:
                    self.render_row(r, now)
            self.text.configure(state="disabled")

            self.update_bit_panel(now)

        if now - self.last_log_status >= 0.5:
            self.last_log_status = now
            n = len(self.log)
            extra = "  (limit reached, oldest dropped)" if n >= LOG_MAX else ""
            self.log_status_var.set(
                f"Log: {n} frames, {len(self.marks)} marks{extra}")

        self.root.after(50, self.tick)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
