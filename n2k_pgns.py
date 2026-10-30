"""
n2k_pgns.py – PGN-Dekoder-Modul für den NMEA2000 Viewer
=========================================================

Übersetzung: Feldnamen, Aufzählungswerte und PGN-Namen laufen automatisch durch i18n.t()
(siehe i18n.py, locales/*.json); Texte ohne Übersetzung bleiben unverändert.

Eigene (z. B. proprietäre) PGNs hinzufügen
------------------------------------------
Lege im Ordner ``pgn_plugins/`` neben diesem Modul eine ``.py``-Datei an
(Dateinamen, die mit ``_`` beginnen, werden ignoriert – siehe
``pgn_plugins/_example_proprietary.py``). Sie wird beim Start bzw. über den
Button „Plugins neu laden" automatisch ausgeführt.

A) Deklarativ (Normalfall)::

    from n2k_pgns import register, F, Repeat

    register(130999, "Mein PGN", fast=True, fields=[
        F("SID",        0, 8, hidden=True),                 # hidden: nur im Detailfenster
        F("Spannung",   8, 16, scale=0.01, unit="V"),
        F("Temperatur", 24, 16, scale=0.01, offset=-273.15, unit="°C"),
        F("Winkel",     40, 16, scale=0.0001, mul=RAD2DEG, unit="°", signed=True),
        F("Modus",      56, 4, kind="enum", enum={0: "Aus", 1: "Ein"}),
        F("Name",       64, 128, kind="str"),
    ])

B) Proprietär (Hersteller-Header in Bit 0..15 wird automatisch dekodiert)::

    register(65280, "Hersteller X: Sensor", manufacturer=275, fields=[
        F("Wert", 16, 16, scale=0.1, unit="bar"),
    ])

   Bit-Offsets zählen immer ab Datenanfang (Bit 0 = LSB von Byte 0).
   Für denselben PGN können mehrere Hersteller registriert werden.

C) Beliebige Python-Funktion (für ungewöhnliche Formate)::

    from n2k_pgns import decoder

    @decoder(126720, "Hersteller Y: Konfiguration", manufacturer=137)
    def _dec(data):
        return [("Befehl", str(data[2])), ("Wert", str(data[3]))]

   Rückgabe: str, dict oder Liste von (Name, Text)-Tupeln.

F-Parameter
-----------
name, bit, length   Position und Länge in Bits
scale, offset       Wert = (raw * scale + offset) * mul
mul                 z. B. RAD2DEG (rad → °) oder MS2KN (m/s → kn)
unit, fmt           Einheit und Formatstring (z. B. "{:.7f}")
signed              vorzeichenbehaftet
kind                "num" (Standard), "enum", "str", "hex", "date", "time", "mfr"
enum                Dict {Zahl: Text} für kind="enum"
hidden              nur im Detailfenster, nicht in der Zeilenansicht
"""
import datetime
import importlib.util
import os

from i18n import t

RAD2DEG = 57.29577951308232
MS2KN = 1.943844492440605

# --------------------------------------------------------------------------
# Hersteller (nicht vollständig – bei Bedarf in einem Plugin ergänzen:
#   from n2k_pgns import MANUFACTURERS; MANUFACTURERS[1234] = "Firma")
# --------------------------------------------------------------------------
MANUFACTURERS = {
    69: "ARKS Enterprises", 78: "FW Murphy", 80: "Twin Disc", 85: "Kohler Power Systems",
    88: "Hemisphere GPS", 116: "BEP Marine", 135: "Airmar", 137: "Maretron", 140: "Lowrance",
    144: "Mercury Marine", 147: "Nautibus", 148: "Blue Water Data", 154: "Westerbeke",
    163: "Evinrude/BRP", 165: "CPAC Systems", 168: "Xantrex", 174: "Volvo Penta",
    176: "Carling Technologies", 185: "Beede Electrical", 192: "Floscan", 198: "Mystic Valley",
    211: "Digital Switching Systems", 224: "EMMI Network", 225: "Honda", 228: "ZF",
    229: "Garmin", 233: "Yacht Monitoring Solutions", 235: "Sailormon", 257: "Honda",
    272: "Groco", 273: "Actisense", 274: "Amphenol LTW", 275: "Navico", 283: "Hamilton Jet",
    285: "Sea Recovery", 286: "Coelmo", 304: "Empirbus", 305: "NovAtel", 306: "Sleipner Motor",
    307: "MBW Technologies", 315: "ICOM", 328: "Qwerty", 341: "Böning Automationstechnologie",
    351: "Thrane & Thrane", 355: "Mastervolt", 356: "Fischer Panda", 358: "Victron Energy",
    370: "Rolls Royce Marine", 373: "Electronic Design", 374: "Northern Lights", 381: "B&G",
    384: "Rose Point Navigation", 385: "Johnson Outdoors", 404: "ComNav", 419: "Fusion Electronics",
    1850: "Teleflex Marine", 1851: "Raymarine", 1852: "Navionics", 1853: "Japan Radio",
    1854: "Northstar", 1855: "Furuno", 1856: "Trimble", 1857: "Simrad", 1858: "Litton",
    1859: "Kvasar", 1860: "MMP", 1861: "Vector Cantech", 1862: "Yamaha Marine",
    1863: "Faria Instruments",
}
INDUSTRY = {0: "Global", 1: "Highway", 2: "Agriculture", 3: "Construction", 4: "Marine", 5: "Industrial"}


def mfr_name(code):
    return "{} ({})".format(MANUFACTURERS.get(code, t("unbekannt")), code)


# --------------------------------------------------------------------------
# Felddefinitionen
# --------------------------------------------------------------------------
def _raw(data, bit, length):
    end = bit + length
    if end > len(data) * 8:
        return None
    b0 = bit // 8
    return (int.from_bytes(data[b0:(end + 7) // 8], "little") >> (bit - b0 * 8)) & ((1 << length) - 1)


class F:
    def __init__(self, name, bit, length, scale=1.0, offset=0.0, mul=1.0, unit="", signed=False,
                 kind="num", enum=None, fmt=None, hidden=False):
        self.name, self.bit, self.length = name, bit, length
        self.scale, self.offset, self.mul = scale, offset, mul
        self.unit, self.signed, self.kind = unit, signed, kind
        self.enum = enum or {}
        self.fmt, self.hidden = fmt, hidden

    def value(self, data, base=0):
        """Text des Feldes oder None, wenn die Daten dafür zu kurz sind."""
        start = base + self.bit
        L = self.length
        if self.kind == "str":
            if start + L > len(data) * 8:
                return None
            s = data[start // 8:(start + L) // 8].split(b"\xff")[0].split(b"\x00")[0]
            return s.decode("latin-1", "replace").strip(" @") or "-"
        raw = _raw(data, start, L)
        if raw is None:
            return None
        if L >= 4:
            top = (1 << (L - 1)) - 1 if self.signed else (1 << L) - 1
            if raw == top:
                return "n/a"
            if L >= 8 and raw == top - 1:
                return "ERR"
        if self.signed and raw >> (L - 1):
            raw -= 1 << L
        k = self.kind
        if k == "enum":
            return t(self.enum.get(raw, "?({})".format(raw)))
        if k == "hex":
            return "0x{:0{}X}".format(raw, (L + 3) // 4)
        if k == "mfr":
            return mfr_name(raw)
        if k == "date":
            try:
                return (datetime.date(1970, 1, 1) + datetime.timedelta(days=raw)).isoformat()
            except OverflowError:
                return "?"
        if k == "time":
            s = raw * 0.0001
            if s >= 86400:
                return "?"
            return "{:02d}:{:02d}:{:06.3f}".format(int(s // 3600), int(s % 3600 // 60), s % 60)
        v = (raw * self.scale + self.offset) * self.mul
        fmt = self.fmt or ("{:.0f}" if (self.scale == 1 and self.offset == 0 and self.mul == 1) else "{:.2f}")
        s = fmt.format(v)
        return "{} {}".format(s, self.unit) if self.unit else s


class Repeat:
    """Sich wiederholende Gruppe (z. B. Satelliten). count: int, (bit, length) oder None (so viele wie passen)."""

    def __init__(self, name, start, item_bits, fields, count=None, max_items=100):
        self.name, self.start, self.item_bits = name, start, item_bits
        self.fields, self.count, self.max_items = fields, count, max_items

    def items(self, data):
        avail = max(0, (len(data) * 8 - self.start) // self.item_bits)
        n = self.count
        if isinstance(n, tuple):
            n = _raw(data, n[0], n[1])
        if n is None or n > avail:
            n = avail
        out = []
        for i in range(min(n, self.max_items)):
            base = self.start + i * self.item_bits
            parts = []
            for f in self.fields:
                v = f.value(data, base)
                if v is not None:
                    parts.append("{}={}".format(t(f.name), v))
            out.append(("{}[{}]".format(t(self.name), i + 1), ", ".join(parts), False))
        return out


_HDR = [F("Hersteller", 0, 11, kind="mfr", hidden=True),
        F("Branche", 13, 3, kind="enum", enum=INDUSTRY, hidden=True)]


class PgnDef:
    def __init__(self, pgn, name, fields=None, fast=None, manufacturer=None, func=None):
        self.pgn, self.name, self.fast = pgn, name, fast
        self.fields, self.manufacturer, self.func = fields or [], manufacturer, func

    def fields_out(self, data):
        """Liste von (Name, Text, hidden)."""
        out = []
        if self.manufacturer is not None:
            for f in _HDR:
                v = f.value(data)
                if v is not None:
                    out.append((t(f.name), v, f.hidden))
        if self.func:
            r = self.func(data)
            if isinstance(r, str):
                r = [("Info", r)]
            elif isinstance(r, dict):
                r = list(r.items())
            for item in r or []:
                out.append((t(item[0]), t(str(item[1])), item[2] if len(item) > 2 else False))
            return out
        for f in self.fields:
            if isinstance(f, Repeat):
                out += f.items(data)
            else:
                v = f.value(data)
                if v is not None:
                    out.append((t(f.name), v, f.hidden))
        return out


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
_DEFS = {}   # pgn -> PgnDef
_PROP = {}   # (pgn, hersteller) -> PgnDef


def is_proprietary(pgn):
    return pgn in (61184, 126720) or 65280 <= pgn <= 65535 or 130816 <= pgn <= 131071


def _mfr_of(data):
    return (data[0] | (data[1] << 8)) & 0x7FF if len(data) >= 2 else None


def register(pgn, name, fields=None, fast=None, manufacturer=None, func=None):
    d = PgnDef(pgn, name, fields, fast, manufacturer, func)
    if manufacturer is not None:
        _PROP[(pgn, manufacturer)] = d
    else:
        _DEFS[pgn] = d
    return d


def decoder(pgn, name, fast=None, manufacturer=None):
    """Dekorator für Python-Dekoderfunktionen."""
    def wrap(fn):
        register(pgn, name, fast=fast, manufacturer=manufacturer, func=fn)
        return fn
    return wrap


def _find(pgn, data):
    if is_proprietary(pgn) and data is not None and len(data) >= 2:
        d = _PROP.get((pgn, _mfr_of(data)))
        if d:
            return d
    return _DEFS.get(pgn)


# Fast-Packet-PGNs ohne eigene Definition
_FAST_EXTRA = {
    126208, 126464, 126720, 126996, 126998, 127233, 127237, 127489, 127496, 127497, 127498,
    127503, 127504, 127506, 127507, 127509, 127510, 127511, 127512, 127513, 127514, 128275,
    129029, 129038, 129039, 129040, 129041, 129044, 129045, 129284, 129285, 129301, 129302,
    129538, 129540, 129541, 129542, 129545, 129547, 129549, 129551, 129556, 129792, 129793,
    129794, 129795, 129796, 129797, 129798, 129799, 129800, 129801, 129802, 129803, 129804,
    129805, 129806, 129807, 129808, 129809, 129810, 130064, 130065, 130066, 130067, 130068,
    130069, 130070, 130071, 130072, 130073, 130074, 130323, 130577, 130578,
}


def is_fast_packet(pgn):
    d = _DEFS.get(pgn)
    if d and d.fast is not None:
        return d.fast
    if pgn == 126720 or 130816 <= pgn <= 131071:
        return True
    return pgn in _FAST_EXTRA


def pgn_name(pgn, data=None):
    d = _find(pgn, data)
    if d:
        return t(d.name)
    if is_proprietary(pgn):
        if data is not None and len(data) >= 2:
            return t("Proprietär") + ": " + MANUFACTURERS.get(_mfr_of(data), t("Hersteller {}").format(_mfr_of(data)))
        return t("Proprietär")
    return ""


def decode_fields(pgn, data):
    """Alle Felder als Liste von (Name, Text) – fürs Detailfenster."""
    try:
        d = _find(pgn, data)
        if d:
            return [(n, v) for n, v, _h in d.fields_out(data)]
        if is_proprietary(pgn) and len(data) >= 2:
            out = [(t(f.name), f.value(data)) for f in _HDR]
            out.append((t("Daten"), data[2:].hex(" ").upper()))
            return out
    except Exception as e:  # fehlerhafte Plugins dürfen den Viewer nicht abstürzen lassen
        return [(t("Dekodierfehler"), "{}: {}".format(type(e).__name__, e))]
    return []


def decode(pgn, data):
    """Kompakter Text für die Tabellenzeile."""
    try:
        d = _find(pgn, data)
        if d:
            return "  ".join(("{}={}".format(n, v) if n else v) for n, v, h in d.fields_out(data) if not h)
        if is_proprietary(pgn) and len(data) >= 2:
            return t("Hersteller {}  Daten {}").format(mfr_name(_mfr_of(data)), data[2:].hex(" ").upper())
    except Exception as e:
        return t("Dekodierfehler: {}: {}").format(type(e).__name__, e)
    return ""


# --------------------------------------------------------------------------
# Plugins
# --------------------------------------------------------------------------
PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pgn_plugins")


def load_plugins(path=None):
    """Lädt alle *.py aus dem Plugin-Ordner. Gibt Liste (Datei, Fehlertext oder None) zurück."""
    path = path or PLUGIN_DIR
    res = []
    if not os.path.isdir(path):
        return res
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("n2k_plugin_" + fn[:-3], os.path.join(path, fn))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            res.append((fn, None))
        except Exception as e:
            res.append((fn, "{}: {}".format(type(e).__name__, e)))
    return res


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
_REF = {0: "wahr", 1: "magnetisch", 2: "Fehler", 3: "n/a"}
_TEMP_SRC = {0: "Meerwasser", 1: "Außen", 2: "Innen", 3: "Maschinenraum", 4: "Hauptkajüte",
             5: "Lebendfischbehälter", 6: "Köderbehälter", 7: "Kühlung", 8: "Heizung", 9: "Taupunkt",
             10: "gefühlt (scheinb. Wind)", 11: "gefühlt (theor. Wind)", 12: "Hitzeindex",
             13: "Gefrierfach", 14: "Abgas"}
_TANK = {0: "Treibstoff", 1: "Frischwasser", 2: "Grauwasser", 3: "Lebendfisch", 4: "Öl",
         5: "Schwarzwasser", 6: "Benzin"}
_DOP_MODE = {0: "1D", 1: "2D", 2: "3D", 3: "Auto", 6: "Fehler", 7: "n/a"}
_GNSS_TYPE = {0: "GPS", 1: "GLONASS", 2: "GPS+GLONASS", 3: "GPS+SBAS/WAAS", 4: "GPS+SBAS+GLONASS",
              5: "Chayka", 6: "integriert", 7: "vermessen", 8: "Galileo"}
_GNSS_METHOD = {0: "kein GNSS", 1: "GNSS-Fix", 2: "DGNSS-Fix", 3: "Precise GNSS", 4: "RTK fix",
                5: "RTK float", 6: "Koppelnavigation", 7: "manuell", 8: "Simulation"}
_AIS_NAV = {0: "unter Maschine", 1: "vor Anker", 2: "manövrierunfähig", 3: "manövrierbehindert",
            4: "durch Tiefgang behindert", 5: "festgemacht", 6: "auf Grund", 7: "Fischerei",
            8: "unter Segel", 14: "AIS-SART", 15: "n/a"}
_MAGVAR_SRC = {0: "manuell", 1: "automatische Karte", 2: "automatische Tabelle", 3: "automatisch berechnet",
               4: "WMM 2000", 5: "WMM 2005", 6: "WMM 2010", 7: "WMM 2015", 8: "WMM 2020"}


def _ang(name, bit, signed=False, **kw):
    return F(name, bit, 16, scale=0.0001, mul=RAD2DEG, unit="°", signed=signed, fmt="{:.1f}", **kw)


def _spd(name, bit, **kw):
    return F(name, bit, 16, scale=0.01, mul=MS2KN, unit="kn", fmt="{:.2f}", **kw)


def _temp(name, bit, length=16, scale=0.01):
    return F(name, bit, length, scale=scale, offset=-273.15, unit="°C", fmt="{:.1f}")


def _lat(bit, length=32, scale=1e-7):
    return F("Breite", bit, length, scale=scale, signed=True, unit="°", fmt="{:.7f}")


def _lon(bit, length=32, scale=1e-7):
    return F("Länge", bit, length, scale=scale, signed=True, unit="°", fmt="{:.7f}")


def _sid():
    return F("SID", 0, 8, hidden=True)


# --------------------------------------------------------------------------
# Python-Dekoder für Sonderfälle
# --------------------------------------------------------------------------
def _pgn_txt(p):
    n = pgn_name(p)
    return "{} ({})".format(p, n) if n else str(p)


def _group_function(d):
    names = {0: "Request", 1: "Command", 2: "Acknowledge", 3: "Read Fields", 4: "Read Fields Reply",
             5: "Write Fields", 6: "Write Fields Reply"}
    out = [("Funktion", names.get(d[0], "?({})".format(d[0])))]
    if len(d) >= 4:
        out.append(("Ziel-PGN", _pgn_txt(int.from_bytes(d[1:4], "little"))))
    if len(d) > 4:
        out.append(("Parameter", d[4:].hex(" ").upper()))
    return out


def _pgn_list(d):
    if not d:
        return []
    fn = {0: "Senden", 1: "Empfangen"}.get(d[0], str(d[0]))
    pgns = [int.from_bytes(d[i:i + 3], "little") for i in range(1, len(d) - 2, 3)]
    return [("Liste", fn), ("PGNs", ", ".join(str(p) for p in pgns))]


def _config_info(d):
    out, i = [], 0
    for lab in ("Installation 1", "Installation 2", "Hersteller-Info"):
        if i + 2 > len(d) or d[i] < 2:
            break
        out.append((lab, d[i + 2:i + d[i]].decode("latin-1", "replace").strip(" @\x00\xff")))
        i += d[i]
    return out


def _switch_bank(d):
    if not d:
        return []
    bits = int.from_bytes(d[1:], "little")
    n = min(28, (len(d) - 1) * 4)
    st = [t("Aus"), t("Ein"), t("Fehler"), "n/a"]
    return [(t("Bank"), str(d[0])), (t("Kanäle"), " ".join("{}:{}".format(i + 1, st[(bits >> (2 * i)) & 3]) for i in range(n)))]


# --------------------------------------------------------------------------
# Standard-PGNs
# --------------------------------------------------------------------------
register(59392, "ISO Acknowledgement", [
    F("Antwort", 0, 8, kind="enum", enum={0: "ACK", 1: "NAK", 2: "Zugriff verweigert", 3: "Adresse belegt"}),
    F("PGN", 24, 24)])
register(59904, "ISO Request", [F("Angefragter PGN", 0, 24)])
register(60928, "ISO Address Claim", [
    F("Seriennr.", 0, 21), F("Hersteller", 21, 11, kind="mfr"),
    F("Geräteinstanz", 32, 8, kind="hex"), F("Gerätefunktion", 40, 8),
    F("Geräteklasse", 49, 7), F("Systeminstanz", 56, 4),
    F("Branche", 60, 3, kind="enum", enum=INDUSTRY)])
register(126208, "NMEA Request/Command/Ack Group Function", fast=True, func=_group_function)
register(126464, "PGN List", fast=True, func=_pgn_list)
register(126992, "System Time", [
    _sid(),
    F("Quelle", 8, 4, kind="enum", enum={0: "GPS", 1: "GLONASS", 2: "Funk", 3: "Cäsium", 4: "Rubidium", 5: "Quarz"}),
    F("Datum", 16, 16, kind="date"), F("Zeit (UTC)", 32, 32, kind="time")])
register(126993, "Heartbeat", [
    F("Intervall", 0, 16, scale=0.001, unit="s", fmt="{:.2f}"), F("Zähler", 16, 8)])
register(126996, "Product Information", [
    F("N2K-Version", 0, 16, scale=0.001, fmt="{:.3f}"), F("Produktcode", 16, 16),
    F("Modell", 32, 256, kind="str"), F("SW-Version", 288, 256, kind="str"),
    F("Modellversion", 544, 256, kind="str"), F("Seriennr.", 800, 256, kind="str"),
    F("Zertifizierung", 1056, 8), F("Load Equivalency", 1064, 8)])
register(126998, "Configuration Information", fast=True, func=_config_info)

register(127245, "Rudder", [
    F("Instanz", 0, 8, hidden=True),
    F("Anweisung", 8, 3, kind="enum", enum={0: "keine", 1: "nach Steuerbord", 2: "nach Backbord"}),
    _ang("Soll", 16, True), _ang("Lage", 32, True)])
register(127250, "Vessel Heading", [
    _sid(), _ang("Kurs", 8), _ang("Deviation", 24, True), _ang("Variation", 40, True),
    F("Referenz", 56, 2, kind="enum", enum=_REF)])
register(127251, "Rate of Turn", [
    _sid(), F("Drehrate", 8, 32, scale=3.125e-8, mul=RAD2DEG, unit="°/s", signed=True, fmt="{:.3f}")])
register(127252, "Heave", [
    _sid(), F("Heave", 8, 16, scale=0.01, unit="m", signed=True), F("Verzögerung", 24, 16, scale=0.01, unit="s")])
register(127257, "Attitude", [_sid(), _ang("Gieren", 8, True), _ang("Nicken", 24, True), _ang("Rollen", 40, True)])
register(127258, "Magnetic Variation", [
    _sid(), F("Quelle", 8, 4, kind="enum", enum=_MAGVAR_SRC), F("Datum", 16, 16, kind="date"),
    _ang("Variation", 32, True)])

register(127488, "Engine Parameters, Rapid", [
    F("Motor", 0, 8), F("Drehzahl", 8, 16, scale=0.25, unit="rpm", fmt="{:.0f}"),
    F("Ladedruck", 24, 16, scale=0.1, unit="kPa", fmt="{:.0f}"), F("Trimm", 40, 8, signed=True, unit="%")])
register(127489, "Engine Parameters, Dynamic", fast=True, fields=[
    F("Motor", 0, 8), F("Öldruck", 8, 16, scale=0.1, unit="kPa", fmt="{:.0f}"),
    _temp("Öltemp", 24, scale=0.1), _temp("Kühlmitteltemp", 40),
    F("Lichtmaschine", 56, 16, scale=0.01, unit="V", signed=True),
    F("Verbrauch", 72, 16, scale=0.1, unit="L/h", signed=True, fmt="{:.1f}"),
    F("Betriebsstunden", 88, 32, scale=1 / 3600, unit="h", fmt="{:.1f}"),
    F("Kühlmitteldruck", 120, 16, scale=0.1, unit="kPa", fmt="{:.0f}"),
    F("Kraftstoffdruck", 136, 16, unit="kPa"),
    F("Status 1", 160, 16, kind="hex"), F("Status 2", 176, 16, kind="hex"),
    F("Last", 192, 8, signed=True, unit="%"), F("Drehmoment", 200, 8, signed=True, unit="%")])
register(127493, "Transmission Parameters, Dynamic", [
    F("Instanz", 0, 8), F("Gang", 8, 2, kind="enum", enum={0: "Vorwärts", 1: "Neutral", 2: "Rückwärts"}),
    F("Öldruck", 16, 16, scale=0.1, unit="kPa", fmt="{:.0f}"), _temp("Öltemp", 32), F("Status", 48, 8, kind="hex")])
register(127501, "Binary Switch Bank Status", func=_switch_bank)
register(127505, "Fluid Level", [
    F("Instanz", 0, 4), F("Typ", 4, 4, kind="enum", enum=_TANK),
    F("Füllstand", 8, 16, scale=0.004, signed=True, unit="%", fmt="{:.1f}"),
    F("Kapazität", 24, 32, scale=0.1, unit="L", fmt="{:.0f}")])
register(127506, "DC Detailed Status", fast=True, fields=[
    _sid(), F("Instanz", 8, 8),
    F("Typ", 16, 8, kind="enum", enum={0: "Batterie", 1: "Lichtmaschine", 2: "Wandler", 3: "Solar", 4: "Windgenerator"}),
    F("Ladezustand", 24, 8, unit="%"), F("Gesundheit", 32, 8, unit="%"),
    F("Restzeit", 40, 16, unit="min"), F("Ripple", 56, 16, scale=0.001, unit="V", fmt="{:.3f}"),
    F("Kapazität", 72, 16, unit="Ah")])
register(127508, "Battery Status", [
    F("Batterie", 0, 8), F("Spannung", 8, 16, scale=0.01, unit="V"),
    F("Strom", 24, 16, scale=0.1, signed=True, unit="A", fmt="{:.1f}"), _temp("Temp", 40), _sid()])

register(128000, "Nautical Leeway Angle", [_sid(), _ang("Abdrift", 8, True)])
register(128259, "Speed", [
    _sid(), _spd("Wasser", 8), _spd("Grund", 24),
    F("Sensor", 40, 8, kind="enum", enum={0: "Schaufelrad", 1: "Pitot", 2: "Doppler", 3: "Korrelation", 4: "Elektromagn."})])
register(128267, "Water Depth", [
    _sid(), F("Tiefe", 8, 32, scale=0.01, unit="m"),
    F("Offset", 40, 16, scale=0.001, signed=True, unit="m", fmt="{:.3f}"), F("Reichweite", 56, 8, scale=10, unit="m")])
register(128275, "Distance Log", fast=True, fields=[
    F("Datum", 0, 16, kind="date"), F("Zeit", 16, 32, kind="time"),
    F("Log", 48, 32, scale=1 / 1852, unit="nm", fmt="{:.2f}"), F("Trip", 80, 32, scale=1 / 1852, unit="nm", fmt="{:.2f}")])

register(129025, "Position, Rapid Update", [_lat(0), _lon(32)])
register(129026, "COG & SOG, Rapid Update", [
    _sid(), F("Referenz", 8, 2, kind="enum", enum=_REF), _ang("COG", 16), _spd("SOG", 32)])
register(129029, "GNSS Position Data", fast=True, fields=[
    _sid(), F("Datum", 8, 16, kind="date"), F("Zeit (UTC)", 24, 32, kind="time"),
    _lat(56, 64, 1e-16), _lon(120, 64, 1e-16),
    F("Höhe", 184, 64, scale=1e-6, signed=True, unit="m", fmt="{:.1f}"),
    F("Typ", 248, 4, kind="enum", enum=_GNSS_TYPE), F("Methode", 252, 4, kind="enum", enum=_GNSS_METHOD),
    F("Integrität", 256, 2, kind="enum", enum={0: "keine", 1: "Safe", 2: "Caution"}),
    F("Satelliten", 264, 8), F("HDOP", 272, 16, scale=0.01, signed=True), F("PDOP", 288, 16, scale=0.01, signed=True),
    F("Geoidhöhe", 304, 32, scale=0.01, signed=True, unit="m"), F("Referenzstationen", 336, 8, hidden=True)])
register(129033, "Time & Date", [
    F("Datum", 0, 16, kind="date"), F("Zeit (UTC)", 16, 32, kind="time"),
    F("Lokaloffset", 48, 16, signed=True, unit="min")])
register(129283, "Cross Track Error", [
    _sid(), F("Querabweichung", 16, 32, scale=0.01, signed=True, unit="m")])
register(129284, "Navigation Data", fast=True, fields=[
    _sid(), F("Distanz zum WP", 8, 32, scale=0.01, unit="m", fmt="{:.0f}"),
    F("ETA", 48, 32, kind="time"), F("ETA-Datum", 80, 16, kind="date"),
    _ang("Peilung Start→Ziel", 96), _ang("Peilung Pos→Ziel", 112),
    F("Start-WP", 128, 32), F("Ziel-WP", 160, 32),
    F("Ziel-Breite", 192, 32, scale=1e-7, signed=True, unit="°", fmt="{:.6f}"),
    F("Ziel-Länge", 224, 32, scale=1e-7, signed=True, unit="°", fmt="{:.6f}"),
    F("Annäherung", 256, 16, scale=0.01, mul=MS2KN, signed=True, unit="kn")])
register(129291, "Set & Drift, Rapid Update", [
    _sid(), F("Referenz", 8, 2, kind="enum", enum={0: "wahr", 1: "magnetisch", 2: "Fehler", 3: "n/a"}),
    _ang("Set", 16), _spd("Drift", 32)])
register(129539, "GNSS DOPs", [
    _sid(), F("Modus gewünscht", 8, 3, kind="enum", enum=_DOP_MODE), F("Modus aktuell", 11, 3, kind="enum", enum=_DOP_MODE),
    F("HDOP", 16, 16, scale=0.01, signed=True), F("VDOP", 32, 16, scale=0.01, signed=True),
    F("TDOP", 48, 16, scale=0.01, signed=True)])
register(129540, "GNSS Satellites in View", fast=True, fields=[
    _sid(), F("Anzahl", 16, 8), Repeat("Sat", 24, 96, [
        F("PRN", 0, 8), F("Elev", 8, 16, scale=0.0001, mul=RAD2DEG, signed=True, unit="°", fmt="{:.0f}"),
        F("Azi", 24, 16, scale=0.0001, mul=RAD2DEG, unit="°", fmt="{:.0f}"),
        F("SNR", 40, 16, scale=0.01, unit="dB", fmt="{:.1f}")], count=(16, 8))])

_AIS_HDR = [F("MMSI", 8, 32)]
register(129038, "AIS Class A Position Report", fast=True, fields=_AIS_HDR + [
    _lat(72), _lon(40), _ang("COG", 112), _spd("SOG", 128), _ang("Heading", 168),
    F("Nav-Status", 200, 4, kind="enum", enum=_AIS_NAV)])
register(129039, "AIS Class B Position Report", fast=True, fields=_AIS_HDR + [
    _lat(72), _lon(40), _ang("COG", 112), _spd("SOG", 128), _ang("Heading", 168)])
register(129794, "AIS Class A Static and Voyage Data", fast=True, fields=_AIS_HDR + [
    F("IMO", 40, 32), F("Rufzeichen", 72, 56, kind="str"), F("Name", 128, 160, kind="str"),
    F("Schiffstyp", 288, 8), F("Schiffslänge", 296, 16, scale=0.1, unit="m", fmt="{:.1f}"),
    F("Schiffsbreite", 312, 16, scale=0.1, unit="m", fmt="{:.1f}"),
    F("Tiefgang", 408, 16, scale=0.01, unit="m"), F("Ziel", 424, 160, kind="str")])
register(129809, "AIS Class B CS Static Data, Part A", fast=True, fields=_AIS_HDR + [F("Name", 40, 160, kind="str")])
register(129810, "AIS Class B CS Static Data, Part B", fast=True, fields=_AIS_HDR + [
    F("Schiffstyp", 40, 8), F("Hersteller-ID", 48, 56, kind="str"), F("Rufzeichen", 104, 56, kind="str"),
    F("Schiffslänge", 160, 16, scale=0.1, unit="m", fmt="{:.1f}"), F("Schiffsbreite", 176, 16, scale=0.1, unit="m", fmt="{:.1f}")])

register(130306, "Wind Data", [
    _sid(), _spd("Geschw.", 8), _ang("Winkel", 24),
    F("Referenz", 40, 3, kind="enum", enum={0: "wahr (Nord)", 1: "magnetisch", 2: "scheinbar",
                                             3: "wahr (Boot)", 4: "wahr (Wasser)"})])
register(130310, "Environmental Parameters", [
    _sid(), _temp("Wasser", 8), _temp("Luft", 24), F("Luftdruck", 40, 16, unit="hPa")])
register(130311, "Environmental Parameters", [
    _sid(), F("Temp-Quelle", 8, 6, kind="enum", enum=_TEMP_SRC),
    F("Feuchte-Quelle", 14, 2, kind="enum", enum={0: "Innen", 1: "Außen"}),
    _temp("Temp", 16), F("Feuchte", 32, 16, scale=0.004, signed=True, unit="%", fmt="{:.1f}"),
    F("Luftdruck", 48, 16, unit="hPa")])
register(130312, "Temperature", [
    _sid(), F("Instanz", 8, 8), F("Quelle", 16, 8, kind="enum", enum=_TEMP_SRC),
    _temp("Ist", 24), _temp("Soll", 40)])
register(130313, "Humidity", [
    _sid(), F("Instanz", 8, 8), F("Quelle", 16, 8, kind="enum", enum={0: "Innen", 1: "Außen"}),
    F("Ist", 24, 16, scale=0.004, signed=True, unit="%", fmt="{:.1f}"),
    F("Soll", 40, 16, scale=0.004, signed=True, unit="%", fmt="{:.1f}")])
register(130314, "Actual Pressure", [
    _sid(), F("Instanz", 8, 8),
    F("Quelle", 16, 8, kind="enum", enum={0: "Atmosphäre", 1: "Wasser", 2: "Dampf", 3: "Druckluft",
                                           4: "Hydraulik", 5: "Filter", 6: "Höhenmesser", 7: "Öl", 8: "Kraftstoff"}),
    F("Druck", 24, 32, scale=0.001, signed=True, unit="hPa", fmt="{:.1f}")])
register(130316, "Temperature, Extended Range", [
    _sid(), F("Instanz", 8, 8), F("Quelle", 16, 8, kind="enum", enum=_TEMP_SRC),
    _temp("Ist", 24, 24, 0.001), _temp("Soll", 48, 16, 0.1)])

register(127497, "Trip Parameters, Engine", fast=True, fields=[
    F("Motor", 0, 8), F("Trip-Verbrauch", 8, 16, unit="L"),
    F("Ø Rate", 24, 16, scale=0.1, signed=True, unit="L/h", fmt="{:.1f}"),
    F("Economy-Rate", 40, 16, scale=0.1, signed=True, unit="L/h", fmt="{:.1f}"),
    F("Momentan", 56, 16, scale=0.1, signed=True, unit="L/h", fmt="{:.1f}")])
register(127513, "Battery Configuration Status", fast=True, fields=[
    F("Batterie", 0, 8),
    F("Typ", 8, 4, kind="enum", enum={0: "Flooded", 1: "Gel", 2: "AGM"}),
    F("Nennspannung", 16, 4, kind="enum", enum={0: "6 V", 1: "12 V", 2: "24 V", 3: "32 V", 4: "62 V", 5: "42 V", 6: "48 V"}),
    F("Chemie", 20, 4, kind="enum", enum={0: "Blei-Säure", 1: "Li-Ion", 2: "NiCd", 3: "ZnO", 4: "NiMH"}),
    F("Kapazität", 24, 16, unit="Ah"), F("Temp.-Koeff.", 40, 8, signed=True, unit="%"),
    F("Peukert", 48, 8, scale=0.002, offset=1, fmt="{:.3f}"),
    F("Ladewirkungsgrad", 56, 8, signed=True, unit="%")])
register(130577, "Direction Data", fast=True, fields=[
    F("Modus", 0, 4, kind="enum", enum={0: "autonom", 1: "differentiell", 2: "geschätzt", 3: "Simulator", 4: "manuell"}),
    F("Referenz", 4, 2, kind="enum", enum=_REF), F("SID", 8, 8, hidden=True),
    _ang("COG", 16), _spd("SOG", 32), _ang("Kurs", 48), _spd("Fahrt durchs Wasser", 64),
    _ang("Set", 80), _spd("Drift", 96)])

# Nur Namen (keine Felddefinition)
for _p, _n in {
    127233: "Man Overboard Notification", 127237: "Heading/Track Control", 127496: "Trip Parameters, Vessel",
    127498: "Engine Parameters, Static", 127503: "AC Input Status",
    127504: "AC Output Status", 127507: "Charger Status", 127509: "Inverter Status",
    127510: "Charger Configuration Status",
    129027: "Position Delta, Rapid Update", 129044: "Datum", 129285: "Navigation – Route/WP Information",
    129301: "Time to/from Mark", 129302: "Bearing and Distance between two Marks",
    129793: "AIS UTC and Date Report", 129040: "AIS Class B Extended Position Report",
    129041: "AIS Aids to Navigation Report", 130578: "Vessel Speed Components",
    65240: "ISO Commanded Address",
}.items():
    register(_p, _n)
