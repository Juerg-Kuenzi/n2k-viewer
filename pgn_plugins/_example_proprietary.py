"""
Beispiel-Plugin für proprietäre PGNs.

Dateien in diesem Ordner, die NICHT mit "_" beginnen, werden beim Start (und über
"Plugins neu laden") ausgeführt. Kopiere diese Datei, benenne sie z. B. in
"meine_firma.py" um und passe sie an.

Die Beispiele unten sind erfundene Demo-Formate – Hersteller-ID 9999 existiert nicht.
"""
from n2k_pgns import register, decoder, F, Repeat, MANUFACTURERS, RAD2DEG

# Eigene Hersteller-ID bekannt machen (11 Bit, 0..2046)
MANUFACTURERS[999] = "Meine Firma"

# 1) Proprietärer Single-Frame-PGN (65280..65535). Bits 0..15 = Hersteller-Header
#    (wird automatisch dekodiert), die Nutzdaten beginnen bei Bit 16.
register(65350, "Meine Firma: Sensorwerte", manufacturer=999, fields=[
    F("Druck", 16, 16, scale=0.1, unit="bar", fmt="{:.1f}"),
    F("Temperatur", 32, 16, scale=0.01, offset=-273.15, unit="°C", fmt="{:.1f}"),
    F("Status", 48, 8, kind="enum", enum={0: "OK", 1: "Warnung", 2: "Fehler"}),
])

# 2) Proprietärer Fast-Packet-PGN (130816..131071) mit wiederholter Gruppe.
register(130900, "Meine Firma: Kanalliste", manufacturer=999, fast=True, fields=[
    F("Anzahl", 16, 8),
    Repeat("Kanal", 24, 24, [
        F("Nr", 0, 8),
        F("Wert", 8, 16, scale=0.1, signed=True, fmt="{:.1f}"),
    ], count=(16, 8)),
])


# 3) Freie Python-Funktion für ungewöhnliche Formate.
#    Rückgabe: Liste von (Name, Text) – optional als 3. Element True = nur im Detailfenster.
@decoder(126720, "Meine Firma: Befehl", manufacturer=999)
def _cmd(data):
    return [("Befehl", "0x{:02X}".format(data[2])),
            ("Nutzdaten", data[3:].hex(" ").upper(), True)]
