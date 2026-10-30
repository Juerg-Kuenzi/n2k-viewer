#!/usr/bin/env python3
"""
NMEA2000 Viewer für den Seeed USB-CAN Analyzer (CH340, 2 Mbaud Seriell-Protokoll)

Start:   python n2k_viewer.py
Benötigt: pip install pyserial

PGN-Dekodierung: n2k_pgns.py  |  eigene/proprietäre PGNs: Ordner pgn_plugins/
"""
import csv
import queue
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import serial
import serial.tools.list_ports

import n2k_pgns as pg

# --------------------------------------------------------------------------
# Seeed USB-CAN Analyzer Protokoll
# --------------------------------------------------------------------------
SPEED_CODES = {
    1000000: 0x01, 800000: 0x02, 500000: 0x03, 400000: 0x04, 250000: 0x05,
    200000: 0x06, 125000: 0x07, 100000: 0x08, 50000: 0x09, 20000: 0x0A,
    10000: 0x0B, 5000: 0x0C,
}
MODES = {"Nur Lauschen (empfohlen)": 2, "Normal": 0}


def build_config(bitrate=250000, mode=2):
    """20-Byte-Konfigurationskommando, Extended Frames (29 Bit)."""
    cmd = bytearray([0xAA, 0x55, 0x12, SPEED_CODES[bitrate], 0x02])
    cmd += (0).to_bytes(4, "little")   # Filter-ID
    cmd += (0).to_bytes(4, "little")   # Masken-ID
    cmd += bytes([mode, 0x01, 0, 0, 0, 0])
    cmd.append(sum(cmd[2:]) & 0xFF)
    return bytes(cmd)


class FrameParser:
    """Zerlegt den Bytestrom in CAN-Frames (variable Länge: AA <info> <id> <data> 55)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        b = self.buf
        while True:
            i = b.find(0xAA)
            if i < 0:
                b.clear()
                break
            if i > 0:
                del b[:i]
            if len(b) < 2:
                break
            info = b[1]
            if (info & 0xC0) != 0xC0:
                del b[0]
                continue
            ext = bool(info & 0x20)
            rtr = bool(info & 0x10)
            dlc = info & 0x0F
            idlen = 4 if ext else 2
            total = 2 + idlen + dlc + 1
            if len(b) < total:
                break
            if b[total - 1] != 0x55:
                del b[0]
                continue
            can_id = int.from_bytes(b[2:2 + idlen], "little")
            payload = bytes(b[2 + idlen:2 + idlen + dlc])
            del b[:total]
            frames.append((can_id, ext, rtr, payload))
        return frames


# --------------------------------------------------------------------------
# NMEA2000
# --------------------------------------------------------------------------
def split_id(can_id):
    prio = (can_id >> 26) & 0x7
    dp = (can_id >> 24) & 0x1
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    sa = can_id & 0xFF
    if pf < 240:
        pgn = (dp << 16) | (pf << 8)
        da = ps
    else:
        pgn = (dp << 16) | (pf << 8) | ps
        da = 255
    return prio, pgn, sa, da


class FastPacket:
    def __init__(self):
        self.state = {}

    def feed(self, sa, pgn, d):
        """Gibt komplette Daten zurück oder None."""
        if not d:
            return None
        key = (sa, pgn)
        fc = d[0] & 0x1F
        if fc == 0:
            if len(d) < 2:
                return None
            total = d[1]
            data = bytearray(d[2:8])
            if len(data) >= total:
                self.state.pop(key, None)
                return bytes(data[:total])
            self.state[key] = {"seq": d[0] >> 5, "total": total, "data": data, "next": 1}
            return None
        st = self.state.get(key)
        if not st or st["seq"] != (d[0] >> 5) or st["next"] != fc:
            self.state.pop(key, None)
            return None
        st["data"] += d[1:8]
        st["next"] += 1
        if len(st["data"]) >= st["total"]:
            self.state.pop(key, None)
            return bytes(st["data"][:st["total"]])
        return None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    TRACE_MAX = 3000

    def __init__(self):
        super().__init__()
        self.title("NMEA2000 Viewer – Seeed USB-CAN Analyzer")
        self.geometry("1250x700")
        self.ser = None
        self.thread = None
        self.running = False
        self.q = queue.Queue()
        self.parser = FrameParser()
        self.fp = FastPacket()
        self.paused = False
        self.log = []                 # (t, can_id, prio, pgn, sa, da, data)
        self.pgn_rows = {}            # (sa,pgn) -> dict
        self.total = 0
        self.last_total = 0
        self.last_tick = time.time()
        self.t0 = time.time()
        self.sel_key = None
        self.iid_key = {}             # Treeview-iid -> (sa, pgn)
        self.plugin_res = pg.load_plugins()
        self._build()
        self.refresh_ports()
        self.after(300, self.show_plugin_errors)
        self.after(100, self.tick)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="COM-Port:").pack(side="left")
        self.port = ttk.Combobox(top, width=28, state="readonly")
        self.port.pack(side="left", padx=4)
        ttk.Button(top, text="⟳", width=3, command=self.refresh_ports).pack(side="left")
        ttk.Label(top, text="  Bitrate:").pack(side="left")
        self.bitrate = ttk.Combobox(top, width=8, state="readonly", values=["250000", "125000", "500000", "1000000"])
        self.bitrate.set("250000")
        self.bitrate.pack(side="left", padx=4)
        ttk.Label(top, text="  Modus:").pack(side="left")
        self.mode = ttk.Combobox(top, width=24, state="readonly", values=list(MODES))
        self.mode.current(0)
        self.mode.pack(side="left", padx=4)
        self.btn_conn = ttk.Button(top, text="Verbinden", command=self.toggle_conn)
        self.btn_conn.pack(side="left", padx=8)
        self.btn_pause = ttk.Button(top, text="Pause", command=self.toggle_pause)
        self.btn_pause.pack(side="left")
        ttk.Button(top, text="Leeren", command=self.clear).pack(side="left", padx=4)
        ttk.Button(top, text="Log speichern (CSV)", command=self.save_log).pack(side="left")
        ttk.Button(top, text="Plugins neu laden", command=self.reload_plugins).pack(side="left", padx=4)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=4)

        # PGN-Übersicht
        f1 = ttk.Frame(nb)
        nb.add(f1, text="PGN-Übersicht")
        pw = ttk.PanedWindow(f1, orient="vertical")
        pw.pack(fill="both", expand=True)
        upper = ttk.Frame(pw)
        pw.add(upper, weight=3)
        cols = ("sa", "pgn", "name", "count", "hz", "data", "dec")
        self.tv = ttk.Treeview(upper, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("sa", "SA", 45), ("pgn", "PGN", 70), ("name", "Name", 200), ("count", "Anzahl", 70),
                        ("hz", "Hz", 55), ("data", "Daten (hex)", 330), ("dec", "Dekodiert", 450)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(upper, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tv.bind("<<TreeviewSelect>>", self.on_select)

        lower = ttk.Frame(pw)
        pw.add(lower, weight=2)
        self.detail_title = ttk.Label(lower, anchor="w")
        self.detail_title.pack(fill="x", padx=2)
        dwrap = ttk.Frame(lower)
        dwrap.pack(fill="both", expand=True)
        self.detail = ttk.Treeview(dwrap, columns=("f", "v"), show="headings", selectmode="none")
        self.detail.heading("f", text="Feld")
        self.detail.heading("v", text="Wert")
        self.detail.column("f", width=240, anchor="w")
        self.detail.column("v", width=700, anchor="w")
        sb3 = ttk.Scrollbar(dwrap, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=sb3.set)
        self.detail.pack(side="left", fill="both", expand=True)
        sb3.pack(side="right", fill="y")
        self.show_detail()

        # Trace
        f2 = ttk.Frame(nb)
        nb.add(f2, text="Trace")
        cols2 = ("t", "id", "prio", "pgn", "sa", "da", "name", "data")
        self.tr = ttk.Treeview(f2, columns=cols2, show="headings")
        for c, t, w in (("t", "Zeit [s]", 80), ("id", "CAN-ID", 90), ("prio", "Prio", 40), ("pgn", "PGN", 70),
                        ("sa", "SA", 45), ("da", "DA", 45), ("name", "Name", 220), ("data", "Daten (hex)", 400)):
            self.tr.heading(c, text=t)
            self.tr.column(c, width=w, anchor="w")
        sb2 = ttk.Scrollbar(f2, orient="vertical", command=self.tr.yview)
        self.tr.configure(yscrollcommand=sb2.set)
        self.tr.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")

        self.status = ttk.Label(self, text="Nicht verbunden", relief="sunken", anchor="w")
        self.status.pack(fill="x", side="bottom")

    # ----- Verbindung -----
    def refresh_ports(self):
        ports = [f"{p.device} – {p.description}" for p in serial.tools.list_ports.comports()]
        self.port["values"] = ports
        if ports and not self.port.get():
            # CH340 bevorzugen
            idx = next((i for i, p in enumerate(ports) if "CH340" in p.upper() or "USB-SERIAL" in p.upper()), 0)
            self.port.current(idx)

    def toggle_conn(self):
        if self.running:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if not self.port.get():
            messagebox.showwarning("Port", "Bitte einen COM-Port wählen.")
            return
        dev = self.port.get().split(" – ")[0]
        try:
            self.ser = serial.Serial(dev, 2000000, timeout=0.05)
            self.ser.write(build_config(int(self.bitrate.get()), MODES[self.mode.get()]))
            self.ser.flush()
        except Exception as e:
            messagebox.showerror("Fehler", f"Verbindung fehlgeschlagen:\n{e}")
            self.ser = None
            return
        self.parser = FrameParser()
        self.running = True
        self.t0 = time.time()
        self.thread = threading.Thread(target=self.reader, daemon=True)
        self.thread.start()
        self.btn_conn.config(text="Trennen")

    def disconnect(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.btn_conn.config(text="Verbinden")

    def reader(self):
        while self.running:
            try:
                data = self.ser.read(4096)
            except Exception as e:
                self.q.put(("error", str(e)))
                break
            if data:
                t = time.time() - self.t0
                for f in self.parser.feed(data):
                    self.q.put(("frame", t, f))
        self.running = False

    # ----- Verarbeitung -----
    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.config(text="Weiter" if self.paused else "Pause")

    def clear(self):
        self.tv.delete(*self.tv.get_children())
        self.tr.delete(*self.tr.get_children())
        self.pgn_rows.clear()
        self.iid_key.clear()
        self.sel_key = None
        self.show_detail()
        self.log.clear()
        self.total = 0
        self.fp = FastPacket()

    def handle_frame(self, t, frame):
        can_id, ext, rtr, d = frame
        if not ext:
            return
        prio, pgn, sa, da = split_id(can_id)
        self.total += 1
        self.log.append((t, can_id, prio, pgn, sa, da, d))
        if len(self.log) > 500000:
            del self.log[:100000]
        name = pg.pgn_name(pgn, None if pg.is_fast_packet(pgn) else d)
        if not self.paused:
            self.tr.insert("", "end", values=(f"{t:.3f}", f"{can_id:08X}", prio, pgn, sa, da, name, d.hex(" ").upper()))
        # Daten der Anwendungsebene
        if pg.is_fast_packet(pgn):
            full = self.fp.feed(sa, pgn, d)
            if full is None:
                return
        else:
            full = d
        r = self.pgn_rows.get((sa, pgn))
        if r is None:
            iid = self.tv.insert("", "end", values=(sa, pgn, name, 0, "", "", ""))
            r = {"iid": iid, "count": 0, "last": None, "hz": 0.0}
            self.pgn_rows[(sa, pgn)] = r
            self.iid_key[iid] = (sa, pgn)
        r["count"] += 1
        if r["last"] is not None and t > r["last"]:
            inst = 1.0 / (t - r["last"])
            r["hz"] = inst if r["hz"] == 0 else 0.8 * r["hz"] + 0.2 * inst
        r["last"] = t
        r["full"] = full
        r["dirty"] = True

    def tick(self):
        n = 0
        try:
            while n < 3000:
                item = self.q.get_nowait()
                n += 1
                if item[0] == "frame":
                    self.handle_frame(item[1], item[2])
                elif item[0] == "error":
                    messagebox.showerror("Serieller Fehler", item[1])
                    self.disconnect()
        except queue.Empty:
            pass
        if not self.paused:
            sel_dirty = False
            for key, r in self.pgn_rows.items():
                if r.get("dirty"):
                    sa, pgn = key
                    full = r["full"]
                    self.tv.item(r["iid"], values=(sa, pgn, pg.pgn_name(pgn, full), r["count"], f"{r['hz']:.1f}",
                                                   full.hex(" ").upper(), pg.decode(pgn, full)))
                    r["dirty"] = False
                    sel_dirty = sel_dirty or key == self.sel_key
            if sel_dirty:
                self.show_detail()
            ch = self.tr.get_children()
            if len(ch) > self.TRACE_MAX:
                self.tr.delete(*ch[:len(ch) - self.TRACE_MAX])
            if ch:
                self.tr.see(self.tr.get_children()[-1])
        now = time.time()
        if now - self.last_tick >= 1.0:
            fps = (self.total - self.last_total) / (now - self.last_tick)
            self.last_total, self.last_tick = self.total, now
            state = "verbunden" if self.running else "nicht verbunden"
            self.status.config(text=f"{state} | Frames: {self.total} | {fps:.0f} Frames/s | PGN/SA-Kombinationen: {len(self.pgn_rows)} | Plugins: {sum(1 for _f, e in self.plugin_res if not e)}")
        self.after(100, self.tick)

    def on_select(self, _event=None):
        sel = self.tv.selection()
        self.sel_key = self.iid_key.get(sel[0]) if sel else None
        self.show_detail()

    def show_detail(self):
        """Alle dekodierten Felder des gewählten PGN im unteren Bereich."""
        r = self.pgn_rows.get(self.sel_key) if self.sel_key else None
        if r is None or "full" not in r:
            self.detail.delete(*self.detail.get_children())
            self.detail_title.config(text="Zeile in der Liste wählen, um alle dekodierten Felder zu sehen")
            return
        sa, pgn = self.sel_key
        full = r["full"]
        rows = pg.decode_fields(pgn, full) or [("(keine Felddefinition)", "siehe pgn_plugins/_example_proprietary.py")]
        self.detail_title.config(text=f"PGN {pgn} – {pg.pgn_name(pgn, full) or 'unbekannt'}   (SA {sa}, {len(full)} Bytes)")
        iids = self.detail.get_children()
        if len(iids) != len(rows):
            self.detail.delete(*iids)
            for row in rows:
                self.detail.insert("", "end", values=row)
        else:
            for iid, row in zip(iids, rows):
                self.detail.item(iid, values=row)

    def show_plugin_errors(self):
        errs = ["{}: {}".format(f, e) for f, e in self.plugin_res if e]
        if errs:
            messagebox.showwarning("Plugin-Fehler", "\n".join(errs))

    def reload_plugins(self):
        self.plugin_res = pg.load_plugins()
        for r in self.pgn_rows.values():
            r["dirty"] = True
        self.show_plugin_errors()
        if not any(e for _f, e in self.plugin_res):
            messagebox.showinfo("Plugins", "{} Plugin(s) geladen.\n(Entfernte Plugins wirken erst nach einem Neustart.)"
                                .format(len(self.plugin_res)))

    def save_log(self):
        if not self.log:
            messagebox.showinfo("Log", "Noch keine Daten.")
            return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                          initialfile=time.strftime("n2k_%Y%m%d_%H%M%S.csv"))
        if not fn:
            return
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Zeit_s", "CAN_ID", "Prio", "PGN", "SA", "DA", "Name", "Daten_hex"])
            for t, cid, prio, pgn, sa, da, d in self.log:
                w.writerow([f"{t:.4f}", f"{cid:08X}", prio, pgn, sa, da, pg.pgn_name(pgn, d), d.hex(" ").upper()])
        messagebox.showinfo("Log", f"{len(self.log)} Frames gespeichert.")

    def on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
