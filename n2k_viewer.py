#!/usr/bin/env python3
"""
NMEA2000 Viewer für den Seeed USB-CAN Analyzer (CH340, 2 Mbaud Seriell-Protokoll)

Start:   python n2k_viewer.py
Benötigt: pip install pyserial

PGN-Dekodierung: n2k_pgns.py  |  eigene/proprietäre PGNs: Ordner pgn_plugins/
"""
import csv
import json
import os
import queue
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import serial
import serial.tools.list_ports

import i18n
import n2k_pgns as pg
from i18n import t as tr          # tr(): Text in die gewählte Sprache übersetzen (siehe i18n.py)

# Einstellungen (Sprache, Port, Bitrate, Modus): %APPDATA%\n2k-viewer.json bzw. ~/n2k-viewer.json
CONFIG_FILE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "n2k-viewer.json")


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError:
        pass

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


def build_status_request():
    """Controller-Status abfragen (ID 0x04, 20 Byte). Die Antwort enthält Byte 3 = Empfangsfehlerzähler (REC)
    und Byte 4 = Sendefehlerzähler (TEC) des CAN-Controllers."""
    cmd = bytearray([0xAA, 0x55, 0x04]) + bytes(16)
    cmd.append(sum(cmd[2:]) & 0xFF)
    return bytes(cmd)


STATUS_REQUEST = build_status_request()

# Fehlerkategorien: Schlüssel -> (Anzeigename, schwerwiegend?). "schwerwiegend" färbt die Anzeige rot,
# solange der letzte Fehler weniger als 10 s zurückliegt; sonst orange. Nicht schwerwiegende nur gelb.
ERR_CATS = {
    "can": ("CAN-Controller", True),
    "fp": ("Fast-Packet", True),
    "serial": ("Serielle Daten", True),
    "fmt": ("Frame-Format", True),
    "nak": ("ISO-NAK", False),
}
WARMUP_S = 2.0       # in den ersten Sekunden nach dem Verbinden sind angeschnittene Frames normal
BADGE_COLORS = {     # Name -> (Hintergrund, Schrift)
    "green": ("#2e7d32", "white"), "yellow": ("#f9a825", "black"), "orange": ("#ef6c00", "white"),
    "red": ("#c62828", "white"), "gray": ("#9e9e9e", "white"),
}
FP_ERRORS = {
    "orphan": "Folgeframe {fc} ohne Startframe",
    "order": "Folgeframe {fc} statt {exp} (Frame verloren)",
    "seq": "Sequenznummer {got} statt {exp} (Nachricht gestört)",
    "restart": "Neue Nachricht begonnen, bevor die vorige vollständig war ({have} von {total} Bytes)",
    "len": "Ungültige Länge {total} Bytes (max. 223)",
}


class FrameParser:
    """Zerlegt den Bytestrom in CAN-Frames (variable Länge: AA <info> <id> <data> 55).

    Erkennt außerdem 20-Byte-Steuerframes (AA 55 ...), z. B. die Antwort auf die Statusabfrage (ID 0x04).
    Ereignisse sammelt er in ``events`` (Abholen mit take_events()):
      ("status", rec, tec)          Fehlerzähler des CAN-Controllers
      ("serial", frames_ohne_ende, verworfene_bytes)   Synchronisationsverlust im Bytestrom (je feed()-Aufruf höchstens einmal)
    """

    def __init__(self):
        self.buf = bytearray()
        self.events = []

    def take_events(self):
        ev, self.events = self.events, []
        return ev

    def feed(self, data):
        self.buf += data
        frames = []
        b = self.buf
        junk = 0          # verworfene Bytes
        bad_end = 0       # Frames ohne Endbyte 0x55
        while True:
            i = b.find(0xAA)
            if i < 0:
                junk += len(b)
                b.clear()
                break
            if i > 0:
                junk += i
                del b[:i]
            if len(b) < 2:
                break
            info = b[1]
            if info == 0x55:                      # 20-Byte-Steuerframe (kein CAN-Datenframe: dort sind Bit 7/6 gesetzt)
                if len(b) < 20:
                    break
                if b[2] == 0x04:
                    self.events.append(("status", b[3], b[4]))
                del b[:20]
                continue
            if (info & 0xC0) != 0xC0:
                junk += 1
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
                bad_end += 1
                del b[0]
                continue
            can_id = int.from_bytes(b[2:2 + idlen], "little")
            payload = bytes(b[2 + idlen:2 + idlen + dlc])
            del b[:total]
            frames.append((can_id, ext, rtr, payload))
        if junk or bad_end:
            self.events.append(("serial", bad_end, junk))
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
    """Setzt Fast-Packet-Nachrichten zusammen und meldet Fehler (Abholen mit take_errors()).
    Eine gestörte Nachricht wird nur einmal gemeldet, nicht einmal pro Folgeframe."""

    def __init__(self):
        self.state = {}
        self.dead = {}        # (sa, pgn) -> Sequenznummer einer bereits gemeldeten, gestörten Nachricht
        self.errors = []      # (Code, Info-Dict) – Codes siehe FP_ERRORS

    def take_errors(self):
        err, self.errors = self.errors, []
        return err

    def _fail(self, key, seq, code, **info):
        self.state.pop(key, None)
        self.dead[key] = seq
        self.errors.append((code, info))

    def feed(self, sa, pgn, d):
        """Gibt komplette Daten zurück oder None."""
        if not d:
            return None
        key = (sa, pgn)
        fc = d[0] & 0x1F
        seq = d[0] >> 5
        if fc == 0:
            if len(d) < 2:
                return None
            self.dead.pop(key, None)
            old = self.state.pop(key, None)
            if old:
                self.errors.append(("restart", {"have": len(old["data"]), "total": old["total"]}))
            total = d[1]
            if total > 223:
                self._fail(key, seq, "len", total=total)
                return None
            data = bytearray(d[2:8])
            if len(data) >= total:
                return bytes(data[:total])
            self.state[key] = {"seq": seq, "total": total, "data": data, "next": 1}
            return None
        st = self.state.get(key)
        if not st:
            if self.dead.get(key) != seq:         # Rest einer schon gemeldeten Nachricht: still verwerfen
                self.errors.append(("orphan", {"fc": fc}))
                self.dead[key] = seq
            return None
        if st["seq"] != seq:
            self._fail(key, st["seq"], "seq", got=seq, exp=st["seq"])
            return None
        if st["next"] != fc:
            self._fail(key, st["seq"], "order", fc=fc, exp=st["next"])
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
    ERR_MAX = 1000

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        i18n.set_language(self.cfg.get("language") or i18n.detect_language())
        self._tx_items = []           # übersetzbare Widget-Texte (für die Live-Umschaltung)
        self.fps = 0.0
        self.title(tr("NMEA2000 Viewer – Seeed USB-CAN Analyzer"))
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
        self.err = {k: 0 for k in ERR_CATS}   # Fehlerzähler je Kategorie
        self.err_last = {}            # Kategorie -> time.time() des letzten Fehlers
        self.err_dirty = False
        self.rate_t = 0.0             # Begrenzung der Fehlerliste (höchstens 20 Einträge/s, Zähler bleiben exakt)
        self.rate_n = 0
        self.hw = None                # CAN-Controller: {'rec','tec','max_rec','max_tec'} oder None
        self.hw_polls = 0
        self.poll_status = bool(self.cfg.get("poll_status", True))
        self.status_interval = float(self.cfg.get("status_interval", 0.25))
        self.plugin_res = pg.load_plugins()
        self._build()
        self.refresh_ports()
        self.after(300, self.show_plugin_errors)
        self.after(100, self.tick)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----- Übersetzung -----
    def _tx(self, widget, src, opt="text"):
        """Widget-Text setzen und für die Live-Umschaltung der Sprache merken."""
        self._tx_items.append(("opt", widget, src, opt))
        widget.config(**{opt: tr(src)})
        return widget

    def _hd(self, tree, col, src):
        self._tx_items.append(("head", tree, col, src))
        tree.heading(col, text=tr(src))

    def _tab(self, nb, frame, src):
        nb.add(frame, text=tr(src))
        self._tx_items.append(("tab", nb, frame, src))

    def apply_language(self):
        self.title(tr("NMEA2000 Viewer – Seeed USB-CAN Analyzer"))
        for kind, w, a, b in self._tx_items:
            if kind == "opt":
                w.config(**{b: tr(a)})
            elif kind == "head":
                w.heading(a, text=tr(b))
            elif kind == "tab":
                w.tab(a, text=tr(b))
        idx = self.mode.current()
        self.mode["values"] = [tr(k) for k in MODES]
        self.mode.current(idx if isinstance(idx, int) and idx >= 0 else 0)
        self.btn_conn.config(text=tr("Trennen") if self.running else tr("Verbinden"))
        self.btn_pause.config(text=tr("Weiter") if self.paused else tr("Pause"))
        for r in self.pgn_rows.values():
            r["dirty"] = True
        self.show_detail()
        self.update_status()
        self.update_errors()

    def on_language(self, _event=None):
        codes = [c for c, _n in i18n.languages()]
        i = self.lang.current()
        if isinstance(i, int) and 0 <= i < len(codes):
            i18n.set_language(codes[i])
            self.cfg["language"] = codes[i]
            save_config(self.cfg)
            self.apply_language()

    def update_status(self):
        state = tr("verbunden") if self.running else tr("nicht verbunden")
        self.status.config(text=tr("{state} | Frames: {n} | {fps:.0f} Frames/s | PGN/SA-Kombinationen: {k} | Plugins: {p}").format(
            state=state, n=self.total, fps=self.fps, k=len(self.pgn_rows),
            p=sum(1 for _f, e in self.plugin_res if not e)))

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        self._tx(ttk.Label(top), "COM-Port:").pack(side="left")
        self.port = ttk.Combobox(top, width=28, state="readonly")
        self.port.pack(side="left", padx=4)
        ttk.Button(top, text="⟳", width=3, command=self.refresh_ports).pack(side="left")
        self._tx(ttk.Label(top), "Bitrate:").pack(side="left", padx=(12, 0))
        self.bitrate = ttk.Combobox(top, width=8, state="readonly", values=["250000", "125000", "500000", "1000000"])
        self.bitrate.set(str(self.cfg.get("bitrate", "250000")))
        self.bitrate.pack(side="left", padx=4)
        self._tx(ttk.Label(top), "Modus:").pack(side="left", padx=(12, 0))
        self.mode = ttk.Combobox(top, width=24, state="readonly", values=[tr(k) for k in MODES])
        mi = self.cfg.get("mode", 0)
        self.mode.current(mi if isinstance(mi, int) and 0 <= mi < len(MODES) else 0)
        self.mode.pack(side="left", padx=4)
        self.btn_conn = ttk.Button(top, text=tr("Verbinden"), command=self.toggle_conn)
        self.btn_conn.pack(side="left", padx=8)
        self.btn_pause = ttk.Button(top, text=tr("Pause"), command=self.toggle_pause)
        self.btn_pause.pack(side="left")
        self._tx(ttk.Button(top, command=self.clear), "Leeren").pack(side="left", padx=4)
        self._tx(ttk.Button(top, command=self.save_log), "Log speichern (CSV)").pack(side="left")
        self._tx(ttk.Button(top, command=self.reload_plugins), "Plugins neu laden").pack(side="left", padx=4)
        # Sprachauswahl (rechts) – Sprachen werden aus locales/*.json erkannt
        langs = i18n.languages()
        self.lang = ttk.Combobox(top, width=12, state="readonly", values=[n for _c, n in langs])
        self.lang.current([c for c, _n in langs].index(i18n.get_language()))
        self.lang.bind("<<ComboboxSelected>>", self.on_language)
        self.lang.pack(side="right")
        self._tx(ttk.Label(top), "Sprache:").pack(side="right", padx=4)

        bar = ttk.Frame(self, padding=(6, 0, 6, 4))
        bar.pack(fill="x")
        self._tx(ttk.Label(bar), "Fehlerzähler:").pack(side="left")
        self.badges = {}
        for key in ERR_CATS:
            lab = tk.Label(bar, padx=8, pady=2, bd=1, relief="solid")
            lab.pack(side="left", padx=(6, 0))
            self.badges[key] = lab
        self._tx(ttk.Button(bar, command=self.reset_errors), "Fehler zurücksetzen").pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=4)

        # PGN-Übersicht
        f1 = ttk.Frame(nb)
        self._tab(nb, f1, "PGN-Übersicht")
        pw = ttk.PanedWindow(f1, orient="vertical")
        pw.pack(fill="both", expand=True)
        upper = ttk.Frame(pw)
        pw.add(upper, weight=3)
        cols = ("sa", "pgn", "name", "count", "hz", "data", "dec")
        self.tv = ttk.Treeview(upper, columns=cols, show="headings", selectmode="browse")
        for c, title, w in (("sa", "SA", 45), ("pgn", "PGN", 70), ("name", "Name", 200), ("count", "Anzahl", 70),
                            ("hz", "Hz", 55), ("data", "Daten (hex)", 330), ("dec", "Dekodiert", 450)):
            self._hd(self.tv, c, title)
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
        self._hd(self.detail, "f", "Feld")
        self._hd(self.detail, "v", "Wert")
        self.detail.column("f", width=240, anchor="w")
        self.detail.column("v", width=700, anchor="w")
        sb3 = ttk.Scrollbar(dwrap, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=sb3.set)
        self.detail.pack(side="left", fill="both", expand=True)
        sb3.pack(side="right", fill="y")
        self.show_detail()

        # Trace
        f2 = ttk.Frame(nb)
        self._tab(nb, f2, "Trace")
        cols2 = ("t", "id", "prio", "pgn", "sa", "da", "name", "data")
        self.tr = ttk.Treeview(f2, columns=cols2, show="headings")
        for c, title, w in (("t", "Zeit [s]", 80), ("id", "CAN-ID", 90), ("prio", "Prio", 40), ("pgn", "PGN", 70),
                            ("sa", "SA", 45), ("da", "DA", 45), ("name", "Name", 220), ("data", "Daten (hex)", 400)):
            self._hd(self.tr, c, title)
            self.tr.column(c, width=w, anchor="w")
        sb2 = ttk.Scrollbar(f2, orient="vertical", command=self.tr.yview)
        self.tr.configure(yscrollcommand=sb2.set)
        self.tr.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")

        # Fehlerliste
        f3 = ttk.Frame(nb)
        nb.add(f3, text=tr("Fehlerliste"))
        self.err_tab = (nb, f3)
        cols3 = ("t", "cat", "sa", "pgn", "msg")
        self.errtv = ttk.Treeview(f3, columns=cols3, show="headings")
        for c, title, w in (("t", "Zeit [s]", 80), ("cat", "Kategorie", 130), ("sa", "SA", 45), ("pgn", "PGN", 70),
                            ("msg", "Details", 700)):
            self._hd(self.errtv, c, title)
            self.errtv.column(c, width=w, anchor="w")
        self.errtv.tag_configure("severe", foreground="#c62828")
        sb4 = ttk.Scrollbar(f3, orient="vertical", command=self.errtv.yview)
        self.errtv.configure(yscrollcommand=sb4.set)
        self.errtv.pack(side="left", fill="both", expand=True)
        sb4.pack(side="right", fill="y")

        self.status = ttk.Label(self, relief="sunken", anchor="w")
        self.status.pack(fill="x", side="bottom")
        self.update_status()
        self.update_errors()

    # ----- Verbindung -----
    def refresh_ports(self):
        ports = [f"{p.device} – {p.description}" for p in serial.tools.list_ports.comports()]
        self.port["values"] = ports
        if ports and not self.port.get():
            want = self.cfg.get("port")      # zuletzt benutzter Port, sonst CH340 bevorzugen
            idx = next((i for i, p in enumerate(ports) if want and p.split(" – ")[0] == want), None)
            if idx is None:
                idx = next((i for i, p in enumerate(ports) if "CH340" in p.upper() or "USB-SERIAL" in p.upper()), 0)
            self.port.current(idx)

    def toggle_conn(self):
        if self.running:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if not self.port.get():
            messagebox.showwarning(tr("Port"), tr("Bitte einen COM-Port wählen."))
            return
        dev = self.port.get().split(" – ")[0]
        try:
            self.ser = serial.Serial(dev, 2000000, timeout=0.05)
            self.ser.write(build_config(int(self.bitrate.get()), MODES[list(MODES)[max(0, self.mode.current())]]))
            self.ser.flush()
        except Exception as e:
            messagebox.showerror(tr("Fehler"), tr("Verbindung fehlgeschlagen:\n{e}").format(e=e))
            self.ser = None
            return
        self.parser = FrameParser()
        self.fp = FastPacket()
        self.hw = None
        self.hw_polls = 0
        self.reset_errors()
        self.running = True
        self.t0 = time.time()
        self.thread = threading.Thread(target=self.reader, daemon=True)
        self.thread.start()
        self.btn_conn.config(text=tr("Trennen"))
        self.cfg.update(port=dev, bitrate=self.bitrate.get(), mode=max(0, self.mode.current()))
        save_config(self.cfg)

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
        self.btn_conn.config(text=tr("Verbinden"))

    def reader(self):
        last_req = 0.0
        while self.running:
            now = time.time()
            if self.poll_status and now - last_req >= self.status_interval:
                last_req = now
                try:
                    self.ser.write(STATUS_REQUEST)     # Befehl an den Adapter, nichts davon geht auf den CAN-Bus
                    self.hw_polls += 1
                except Exception as e:
                    self.q.put(("error", str(e)))
                    break
            try:
                data = self.ser.read(4096)
            except Exception as e:
                self.q.put(("error", str(e)))
                break
            if data:
                t = time.time() - self.t0
                for f in self.parser.feed(data):
                    self.q.put(("frame", t, f))
                for ev in self.parser.take_events():
                    if ev[0] == "status":
                        self.q.put(("hw", t, ev[1], ev[2]))
                    elif ev[0] == "serial":
                        self.q.put(("serial", t, ev[1], ev[2]))
        self.running = False

    # ----- Verarbeitung -----
    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.config(text=tr("Weiter") if self.paused else tr("Pause"))

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
            self.add_error(t, "fmt", tr("Standard-Frame (11 Bit) ID 0x{id:03X} – kein NMEA2000").format(id=can_id))
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
            for code, info in self.fp.take_errors():
                if code == "orphan" and t < WARMUP_S:      # beim Verbinden mitten in einer Nachricht ist das normal
                    continue
                self.add_error(t, "fp", tr(FP_ERRORS[code]).format(**info), sa, pgn)
            if full is None:
                return
        else:
            full = d
        if pgn == 59392 and len(full) >= 8 and full[0] in (1, 2, 3):      # ISO Acknowledgement: NAK / verweigert / belegt
            what = {1: "NAK", 2: "Zugriff verweigert", 3: "Adresse belegt"}[full[0]]
            self.add_error(t, "nak", tr("{what} für PGN {pgn} (Antwort an Adresse {da})").format(
                what=tr(what), pgn=int.from_bytes(full[5:8], "little"), da=da), sa, pgn)
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
                elif item[0] == "hw":
                    self.on_hw_status(item[1], item[2], item[3])
                elif item[0] == "serial":
                    if item[1] >= WARMUP_S:                     # angeschnittene Bytes direkt nach dem Verbinden ignorieren
                        self.add_error(item[1], "serial", tr(
                            "{bad} Frame(s) ohne Endbyte, {junk} Byte(s) verworfen (Synchronisation verloren)").format(
                            bad=item[2], junk=item[3]))
                elif item[0] == "error":
                    messagebox.showerror(tr("Serieller Fehler"), item[1])
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
        if self.err_dirty:
            self.err_dirty = False
            self.update_errors()
        now = time.time()
        if now - self.last_tick >= 1.0:
            fps = (self.total - self.last_total) / (now - self.last_tick)
            self.last_total, self.last_tick = self.total, now
            self.fps = fps
            self.update_status()
            self.update_errors()      # Farbe wechselt nach 10 s von rot auf orange
        self.after(100, self.tick)

    # ----- Fehleranzeige -----
    def add_error(self, t, cat, text, sa=None, pgn=None):
        """Fehler zählen und (begrenzt) in die Fehlerliste eintragen."""
        self.err[cat] += 1
        now = time.time()
        self.err_last[cat] = now
        if now - self.rate_t >= 1.0:
            self.rate_t, self.rate_n = now, 0
        self.rate_n += 1
        if self.rate_n <= 20:
            self.errtv.insert("", "end", values=(f"{t:.3f}", tr(ERR_CATS[cat][0]), "" if sa is None else sa,
                                                 "" if pgn is None else pgn, text),
                              tags=("severe" if ERR_CATS[cat][1] else "info",))
            ch = self.errtv.get_children()
            if len(ch) > self.ERR_MAX:
                self.errtv.delete(*ch[:len(ch) - self.ERR_MAX])
            self.errtv.see(ch[-1])
        self.err_dirty = True

    def on_hw_status(self, t, rec, tec):
        """Fehlerzähler des CAN-Controllers (Antwort des Adapters auf die Statusabfrage)."""
        prev = self.hw
        self.hw = {"rec": rec, "tec": tec,
                   "max_rec": max(rec, prev["max_rec"] if prev else 0), "max_tec": max(tec, prev["max_tec"] if prev else 0)}
        if prev is None:
            if rec or tec:
                self.add_error(t, "can", tr("Zählerstand bei erster Abfrage: TEC {tec} / REC {rec}").format(tec=tec, rec=rec))
        else:
            if rec > prev["rec"]:
                self.add_error(t, "can", tr("Empfangsfehlerzähler (REC) gestiegen: {a} → {b}").format(a=prev["rec"], b=rec))
            if tec > prev["tec"]:
                self.add_error(t, "can", tr("Sendefehlerzähler (TEC) gestiegen: {a} → {b}").format(a=prev["tec"], b=tec))
        self.err_dirty = True

    def reset_errors(self):
        for k in self.err:
            self.err[k] = 0
        self.err_last.clear()
        self.errtv.delete(*self.errtv.get_children())
        if self.hw:
            self.hw["max_rec"], self.hw["max_tec"] = self.hw["rec"], self.hw["tec"]
        self.update_errors()

    @staticmethod
    def _badge(label, text, color):
        bg, fg = BADGE_COLORS[color]
        label.config(text=text, bg=bg, fg=fg)

    def update_errors(self):
        now = time.time()
        name = tr(ERR_CATS["can"][0])
        if self.hw is None:
            if self.poll_status and self.hw_polls >= 8:
                self._badge(self.badges["can"], name + ": " + tr("Adapter meldet keine Zähler"), "gray")
            else:
                self._badge(self.badges["can"], name + ": TEC – / REC –", "gray")
        else:
            h = self.hw
            level = max(h["rec"], h["tec"])          # CAN: >=96 Warnung, >=128 Error-Passive
            color = "green" if level == 0 else "yellow" if level < 96 else "orange" if level < 128 else "red"
            self._badge(self.badges["can"], name + ": " + tr("TEC {tec} / REC {rec}  (Max {mt} / {mr})").format(
                tec=h["tec"], rec=h["rec"], mt=h["max_tec"], mr=h["max_rec"]), color)
        for key, (label, severe) in ERR_CATS.items():
            if key == "can":
                continue
            n = self.err[key]
            if n == 0:
                color = "green"
            elif not severe:
                color = "yellow"
            else:
                color = "red" if now - self.err_last.get(key, 0) < 10 else "orange"
            self._badge(self.badges[key], "{}: {}".format(tr(label), n), color)
        total = sum(self.err.values())
        nb, frame = self.err_tab
        nb.tab(frame, text=tr("Fehlerliste") + (" ({})".format(total) if total else ""))

    def on_select(self, _event=None):
        sel = self.tv.selection()
        self.sel_key = self.iid_key.get(sel[0]) if sel else None
        self.show_detail()

    def show_detail(self):
        """Alle dekodierten Felder des gewählten PGN im unteren Bereich."""
        r = self.pgn_rows.get(self.sel_key) if self.sel_key else None
        if r is None or "full" not in r:
            self.detail.delete(*self.detail.get_children())
            self.detail_title.config(text=tr("Zeile in der Liste wählen, um alle dekodierten Felder zu sehen"))
            return
        sa, pgn = self.sel_key
        full = r["full"]
        rows = pg.decode_fields(pgn, full) or [(tr("(keine Felddefinition)"), tr("siehe pgn_plugins/_example_proprietary.py"))]
        self.detail_title.config(text=tr("PGN {pgn} – {name}   (SA {sa}, {n} Bytes)").format(
            pgn=pgn, name=pg.pgn_name(pgn, full) or tr("unbekannt"), sa=sa, n=len(full)))
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
            messagebox.showwarning(tr("Plugin-Fehler"), "\n".join(errs))

    def reload_plugins(self):
        self.plugin_res = pg.load_plugins()
        for r in self.pgn_rows.values():
            r["dirty"] = True
        self.show_plugin_errors()
        if not any(e for _f, e in self.plugin_res):
            messagebox.showinfo(tr("Plugins"), tr("{n} Plugin(s) geladen.\n(Entfernte Plugins wirken erst nach einem Neustart.)")
                                .format(n=len(self.plugin_res)))

    def save_log(self):
        if not self.log:
            messagebox.showinfo(tr("Log"), tr("Noch keine Daten."))
            return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                          initialfile=time.strftime("n2k_%Y%m%d_%H%M%S.csv"))
        if not fn:
            return
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([tr(h) for h in ("Zeit_s", "CAN_ID", "Prio", "PGN", "SA", "DA", "Name", "Daten_hex")])
            for t, cid, prio, pgn, sa, da, d in self.log:
                w.writerow([f"{t:.4f}", f"{cid:08X}", prio, pgn, sa, da, pg.pgn_name(pgn, d), d.hex(" ").upper()])
        messagebox.showinfo(tr("Log"), tr("{n} Frames gespeichert.").format(n=len(self.log)))

    def on_close(self):
        save_config(self.cfg)
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
