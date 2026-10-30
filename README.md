# n2k-viewer

NMEA2000-Viewer für Windows mit dem **Seeed USB-CAN Analyzer** (CH340, serielles 2-Mbaud-Protokoll).
Zeigt alle Nachrichten eines NMEA2000-Busses als PGN-Übersicht (mit Frequenz und dekodierten Werten) und als Trace.

## Start

```
pip install pyserial
python n2k_viewer.py
```

COM-Port wählen, Bitrate 250 kbit/s (NMEA2000-Standard), Modus „Nur Lauschen“ (sendet nichts auf den Bus), „Verbinden“.

## Funktionen

- **PGN-Übersicht:** pro Quelladresse und PGN eine Zeile mit Anzahl, Hz, Rohdaten und dekodiertem Text.
  Zeile anklicken → unten alle Felder einzeln, live aktualisiert.
- **Trace:** alle Frames mit Zeitstempel, CAN-ID, Priorität, PGN, SA, DA.
- **Fast-Packet** wird zusammengesetzt (z. B. 129029, 126996).
- **Log-Export** als CSV.

## Dateien

| Datei | Zweck |
|---|---|
| `n2k_viewer.py` | GUI (Tkinter), Seeed-Protokoll, Fast-Packet-Zusammensetzung |
| `n2k_pgns.py` | PGN-Definitionen und Decoder (ca. 60 PGNs), Plugin-Loader |
| `pgn_plugins/` | eigene / proprietäre PGNs als Plugins |

## Eigene (proprietäre) PGNs hinzufügen

`pgn_plugins/_example_proprietary.py` unter einem Namen ohne führenden Unterstrich kopieren
(z. B. `meine_firma.py`) und anpassen. Beim Start bzw. über „Plugins neu laden“ wird sie geladen.

```python
from n2k_pgns import register, F, MANUFACTURERS
MANUFACTURERS[999] = "Meine Firma"

register(65350, "Meine Firma: Sensorwerte", manufacturer=999, fields=[
    F("Druck",      16, 16, scale=0.1,  unit="bar"),
    F("Temperatur", 32, 16, scale=0.01, offset=-273.15, unit="°C"),
    F("Status",     48, 8,  kind="enum", enum={0: "OK", 1: "Warnung"}),
])
```

Der Hersteller-Header (Bit 0–15) wird automatisch dekodiert, Nutzdaten beginnen bei Bit 16.
Wiederholte Gruppen: `Repeat`; Sonderfälle: `@decoder`. Details im Kopf von `n2k_pgns.py`.

## Hinweise

- Die Bitlayouts der Standard-PGNs sind nach bestem Wissen umgesetzt, aber nicht gegen die offizielle
  Spezifikation geprüft. Bei Abweichungen bitte ein Issue mit einem Mitschnitt anlegen.
- Der Seeed-Analyzer ist nicht galvanisch getrennt; Bus korrekt terminieren (CAN_H, CAN_L, GND).
